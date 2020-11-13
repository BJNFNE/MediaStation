#!/usr/bin/python3

import argparse
import logging

from enum import IntEnum
import struct
import io
from pathlib import Path
import os
import subprocess
import mmap

import PIL.Image as PILImage

class RecordType(IntEnum):
    CXT      = 0x0006,
    FILE_1   = 0x0008,
    FILE_3   = 0x0029,
    RIFF     = 0x0028,
    CURSOR   = 0x0015,
    RES_NAME = 0x0bba,
    RES_ID   = 0x0bbb,

class BootRecord(IntEnum):
    FILES_1  = 0x0002,
    FILES_2  = 0x0007,
    FILES_3  = 0x000a,
    RIFF     = 0x000b,
    CURSOR   = 0x000c,

class ChunkType(IntEnum):
    HEADER         = 0x000d,
    IMAGE          = 0x0018,
    MOVIE_FRAME    = 0x06a9,
    MOVIE_HEADER   = 0x06aa,

class HeaderType(IntEnum):
    ROOT    = 0x000e,
    PALETTE = 0x05aa,
    ASSET   = 0x0011,
    LINK    = 0x0013,

class AssetType(IntEnum):
    SCR  = 0x0001,
    STG  = 0x0002,
    SND  = 0x0005,
    TMR  = 0x0006,
    IMG  = 0x0007,
    HSP  = 0x000b,
    SPR  = 0x000e,
    MOV  = 0x0016,
    PAL  = 0x0017,
    TXT  = 0x001a,
    FON  = 0x001b,
    CVS  = 0x001e,

class DatumType(IntEnum):
    UINT8   = 0x0002,
    UINT16  = 0x0003,
    UINT32  = 0x0004,
    UINT32_2  = 0x0007,
    UINT16_2  = 0x0006,
    UINT16_3  = 0x0013,
    UINT64  = 0x0009,
    SINT16  = 0x0010,
    SINT64  = 0x0011,
    STRING  = 0x0012,
    FILE    = 0x000a,
    POINT   = 0x000f,
    POINT_2 = 0x000e,
    PALETTE = 0x05aa,
    REF     = 0x001b,
    BBOX    = 0x000d,
    POLY    = 0x001d,

def chunk_int(chunk):
    try:
        return int(chunk['code'][1:], 16)
    except Exception as e:
        return None

def read_chunk(stream):
    if stream.tell() % 2 == 1:
        stream.read(1)

    chunk = {
        "start": stream.tell(),
        "code": stream.read(4).decode("utf-8"),
        "size": struct.unpack("<L", stream.read(4))[0]
    }

    logging.debug("(@0x{:012x}) Read chunk {} (0x{:04x} bytes)".format(stream.tell(), chunk["code"], chunk["size"]))
    return chunk

def read_riff(stream, full=True):
    start = stream.tell()
    if full:
        value_assert(stream, b'RIFF', "signature")
        size1 = struct.unpack("<L", stream.read(4))[0]

    value_assert(stream, b'IMTS', "signature")
    value_assert(stream, b'rate', "signature")

    unk1 = Datum(stream)
    stream.read(2) # 00 00

    value_assert(stream, b'LIST', "signature")
    size2 = struct.unpack("<L", stream.read(4))[0]
    # assert size1 - size2 == 0x24, "Unexpected chunk size"

    value_assert(stream, b'data', "signature")
    return size2 + (stream.tell() - start) - 8

def read_init(stream):
    stream.seek(0)

    assert stream.read(4) == b'II\x00\x00', "Incorrect file signature"
    struct.unpack("<L", stream.read(4))[0]
    riffs = struct.unpack("<L", stream.read(4))[0]
    total = struct.unpack("<L", stream.read(4))[0]

    return riffs, total

def value_assert(stream, target, type="value", warn=False):
    s = 0
    ax = stream
    try:
        s = stream.tell()
        ax = stream.read(len(target))
    except AttributeError:
        pass

    msg = "(@ +0x{:0>4x}) Expected {} {}{}, received {}{}".format(
        s, type, target, " (0x{:0>4x})".format(target) if isinstance(target, int) else "",
        ax, " (0x{:0>4x})".format(ax) if isinstance(ax, int) else "",
    )
    if warn and ax != target:
        logging.warning(msg)
    else:
        assert ax == target, msg

        return ax


############### INTERNAL DATA REPRESENTATIONS ############################

class Object:
    def __format__(self, spec):
        return self.__repr__()

class Datum(Object):
    def __init__(self, stream, parent=None, peek=False):
        # TODO: All this processing is unnecessary once I have the format
        # completely figured out. All of this complicated type-checking can be
        # replaced with assertion.
        self.start = stream.tell()
        self.d = None
        self.t = struct.unpack("<H", stream.read(2))[0]

        if self.t == DatumType.UINT8:
            self.d = int.from_bytes(stream.read(1), byteorder='little')
        elif self.t == DatumType.UINT16 or self.t == DatumType.UINT16_2 or self.t == DatumType.UINT16_3:
            self.d = struct.unpack("<H", stream.read(2))[0]

            if self.d == DatumType.POLY:
                self.t = self.d
                self.d = Polygon(stream)
        elif self.t == DatumType.SINT16:
            self.d = struct.unpack("<H", stream.read(2))[0]
        elif self.t == DatumType.UINT32 or self.t == DatumType.UINT32_2:
            self.d = struct.unpack("<L", stream.read(4))[0]
        elif self.t == DatumType.UINT64:
            self.d = struct.unpack("<Q", stream.read(8))[0]
        elif self.t == DatumType.SINT64:
            self.d = struct.unpack("<q", stream.read(8))[0]
        elif self.t == DatumType.STRING or self.t == DatumType.FILE:
            size = Datum(stream)
            self.d = stream.read(size.d).decode("utf-8")
        elif self.t == DatumType.BBOX:
            self.d = Bbox(stream)
        elif self.t == DatumType.POINT or self.t == DatumType.POINT_2:
            self.d = Point(stream)
        elif self.t == DatumType.REF:
            self.d = Ref(stream, parent.type)
        else:
            stream.seek(stream.tell() - 2)
            raise TypeError(
                "(@ 0x{:0>12x}) Unknown datum type 0x{:0>4x}".format(stream.tell(), self.t)
            )

        if peek: stream.seek(self.start)

    def __repr__(self):
        data = ""
        base = "<Datum: +0x{:0>4x}; type: 0x{:0>4x}, ".format(
            self.start, self.t
        )
        
        try:
            if len(self.d) > 0x12 and not isinstance(self.d, str):
                data = "<length: 0x{:0>6x}>>".format(len(self.d))
            else:
                data = "data: {}>".format(self.d)
        except:
            data = "data: {}{:0>6x}{}>".format(
                "0x" if isinstance(self.d, int) else "", self.d,
                " ({:0>4d})".format(self.d) if isinstance(self.d, int) else ""
            )
            
        return base + data

class Ref(Object):
    def __init__(self, stream, type):
        self.refs = []

        if type.d in (AssetType.SPR, AssetType.IMG, AssetType.SND, AssetType.FON):
            self.append(stream)
        elif type.d == AssetType.MOV:
            self.append(stream)
            stream.read(2)

            self.append(stream)
            stream.read(2)

            self.append(stream)
        else:
            raise ValueError("Reference for unexpected asset type: {}".format(type.d))

    def append(self, stream):
        self.refs.append(
            (stream.read(4).decode("utf-8"), Datum(stream))
        )

    def id(self, string=False):
        return [int(ref[0][1:], 16) if string else ref[0] for ref in self.refs]

    def __repr__(self):
        return "<Ref: {})>".format([(s, i) for s, i in zip(self.id(), self.id(string=True))])

class Point(Object):
    def __init__(self, m):
        value_assert(m, b'\x10\x00', "chunk")
        self.x = struct.unpack("<H", m.read(2))[0]

        value_assert(m, b'\x10\x00', "chunk")
        self.y = struct.unpack("<H", m.read(2))[0]

    def __repr__(self):
        return "<Point: x: {}, y: {}>".format(self.x, self.y)

class Bbox(Object):
    def __init__(self, m):
        value_assert(m, b'\x0e\x00', "chunk")
        self.point = Point(m)

        value_assert(m, b'\x0f\x00', "chunk")
        self.dims = Point(m)

    def __repr__(self):
        return "<Bbox: {}, {}, {}, {}>".format(
            self.point.x, self.point.x + self.dims.x,
            self.point.y, self.point.y + self.dims.y
        )

class Polygon(Object):
    def __init__(self, m):
        size = Datum(m)

        self.points = []
        while m.read(2) == b'\x0e\x00':
            self.points.append(Point(m))

        m.seek(m.tell() - 2)
        value_assert(size.d, len(self.points), "polygon points", warn=True)

    def __repr__(self):
        return "<Polygon: points: {}>".format(len(self.points))

class Array(Object):
    def __init__(self, stream, parent=None, bytes=None, datums=None, stop=None):
        if not datums and not bytes and not stop:
            raise AttributeError("Creating an array requires providing a bytes size or a stop parameter.")

        start = stream.tell()

        self.datums = []
        while True:
            d = Datum(stream, parent=parent if parent else self)
            if stop and d.t == stop[0] and d.d == stop[1]:
                break

            self.datums.append(d)
            if bytes and stream.tell() >= bytes + start:
                break
            if datums and len(self.datums) == datums:
                break

        logging.debug("Read 0x{:04x} array bytes".format(stream.tell() - start))

    def log(self):
        logging.debug(self)
        for datum in self.datums:
            logging.debug(" -> {}".format(datum))

    def __repr__(self):
        return "<Array: size: {:0>4d}>".format(len(self.datums))

class AssetHeader(Object):
    def __init__(self, stream, **kwargs):
        start = stream.tell()
        self.data = Array(stream, datums=4)

        if self.data.datums[1].d == AssetType.PAL:
            self.data.datums.append(Datum(stream))
            value_assert(Datum(stream).d, DatumType.PALETTE, "palette signature")
            self.child = stream.read(0x300)
        elif self.data.datums[1].d == AssetType.STG:
            assert kwargs['size']
            self.data.datums += Array(stream, parent=self, stop=(DatumType.UINT16, 0x0000)).datums

            value_assert(Datum(stream).d, HeaderType.LINK, "link signature")
            value_assert(Datum(stream).d, self.id.d, "asset id")

            self.child = []
            if Datum(stream, peek=True).d == HeaderType.ASSET:
                Datum(stream)
                while stream.tell() < start + kwargs['size']:
                    self.child.append(AssetHeader(stream, size=start+kwargs['size']-stream.tell(), stop=(DatumType.UINT16, HeaderType.ASSET)))
                    logging.debug(" -> {}".format(self.child[-1]))
        else:
            self.child = None
            self.data.datums += Array(stream, parent=self, bytes=start+kwargs.get('size')-stream.tell(), stop=kwargs.get('stop')).datums

        if kwargs.get('size') and not kwargs.get('stop') and stream.tell() < start + kwargs['size']:
            logging.warning("{} bytes left in asset header".format(start+kwargs['size']-stream.tell()))
            stream.read(start + kwargs['size'] - stream.tell())

    @property
    def type(self):
        return self.data.datums[1]

    @property
    def id(self):
        return self.data.datums[2]

    @property
    def name(self):
        for datum in self.data.datums:
            if datum.t == DatumType.STRING:
                return datum

    @property
    def ref(self):
        # TODO: Enumerate all types that have associated data chunks
        r = None
        if self.type.d in (AssetType.SND, AssetType.FON):
            r = self.data.datums[6]
        elif self.type.d in (AssetType.MOV, AssetType.IMG, AssetType.SPR):
            r = self.data.datums[8]

        return r if r and r.t == DatumType.REF else None

    def __repr__(self):
        return "<AssetHeader: type: 0x{:0>4x}, id: 0x{:0>4x} ({:0>4d}){}{}>".format(
            self.type.d, self.id.d, self.id.d,
            " {}".format(self.ref.d) if self.ref else "",
            ", name: {}".format(self.name.d) if self.name else ""
        )

class AssetLink(Object):
    def __init__(self, stream, size):
        # TODO: Determine the lengths of these asset links.
        self.type = Datum(stream)
        self.data = Array(stream, bytes=size-0x04)

    @property
    def ids(self):
        # For now, we just skip the even indices, as these are delimiters.
        return self.data.datums[::2]


############### EXTERNAL DATA REPRESENTATIONS ############################

class Image(Object):
    def __init__(self, stream, size, dims=None, sprite=False):
        start = stream.tell()
        self.check = not sprite

        self.header = None
        self.dims = dims
        if not dims:
            value_assert(Datum(stream).d, ChunkType.IMAGE, "image signature")
            self.header = Array(stream, bytes=0x16-0x04)

        logging.debug("Reading 0x{:04x} raw image bytes".format(size + start - stream.tell()))
        self.raw = io.BytesIO(stream.read(size + start - stream.tell()))
        self.offset = 0

    @property
    def image(self):
        self.raw.seek(0)
        if self.check: value_assert(self.raw, b'\x00\x00', "image row header")

        if not self.compressed:
            return self.raw.read()

        done = False
        image = bytearray((self.width*self.height) * b'\x00')
        for h in range(self.height):
            self.offset = 0
            while True:
                code = int.from_bytes(self.raw.read(1), byteorder='little')

                if code == 0x00: # control mode
                    op = int.from_bytes(self.raw.read(1), byteorder='little')
                    if op == 0x00: # end of line
                        break

                    if op == 0x01: # end of image
                        done = True
                        break

                    if op == 0x03: # offset for RLE
                        self.offset += struct.unpack("<H", self.raw.read(2))[0]
                    else: # uncompressed data of given length
                        pix = self.raw.read(op)

                        loc = (h * self.width) + self.offset
                        image[loc:loc+len(pix)] = pix

                        if self.raw.tell() % 2 == 1:
                            self.raw.read(1)

                        self.offset += len(pix)
                else: # RLE data
                    loc = (h * self.width) + self.offset

                    pix = self.raw.read(1)
                    image[loc:loc+code] = code * pix

                    self.offset += code

            if done: break

        value_assert(len(image), self.width*self.height, "image length ({} x {})".format(self.width, self.height), warn=True)
        return bytes(image)

    def export(self, directory, filename, fmt="png", **kwargs):
        filename = os.path.join(directory, filename)

        if self.width == 0 and self.height == 0:
            logging.warning("Found image with length and width 0, skipping export")
            return

        if filename[-4:] != ".{}".format(fmt):
            filename += (".{}".format(fmt))

        image = PILImage.frombytes("P", (self.width, self.height), self.image)
        if 'palette' in kwargs and kwargs['palette']:
            image.putpalette(kwargs['palette'])

        image.save(filename, fmt)

    @property
    def compressed(self):
        return bool((self.header and self.header.datums[1].d) or self.dims)

    @property
    def width(self):
        return self.dims.d.x if self.dims else self.header.datums[0].d.x

    @property
    def height(self):
        return self.dims.d.y if self.dims else self.header.datums[0].d.y

    def __repr__(self):
        return "<Image: size: {} x {}>".format(self.width, self.height)

class MovieFrame(Object):
    def __init__(self, stream, size):
        start = stream.tell()
        size -= 0x04

        self.header = Array(stream, bytes=0x22)

        if size - (stream.tell() - start) == 0x02:
            stream.read(2)
            self.image = None
        else:
            self.image = Image(stream, size=size+start-stream.tell(), dims=self.header.datums[1]) if size - (stream.tell() - start) > 0x02 else None

class Movie(Object):
    def __init__(self, stream, chunk, size, stills=None):
        self.stills = stills
        self.chunks = []

        start = stream.tell()
        codes = {
            "header": chunk_int(chunk),
            "video" : chunk_int(chunk) + 1,
            "audio" : chunk_int(chunk) + 2,
        }

        while stream.tell() <= start + size:
            if not chunk_int(chunk):
                break

            frames = []
            frame_headers = []
            header = Array(stream, bytes=chunk['size'])

            if stream.tell() >= start + size:
                break

            chunk = read_chunk(stream)
            if not chunk:
                break

            while chunk_int(chunk) == codes["video"]:
                type = Datum(stream)

                if type.d == ChunkType.MOVIE_FRAME:
                    frames.append(MovieFrame(stream, size=chunk['size']))
                elif type.d == ChunkType.MOVIE_HEADER:
                    frame_headers.append(Array(stream, bytes=chunk['size']-0x04))

                chunk = read_chunk(stream)

            self.chunks.append({
                "header": header,
                "frames": list(zip(frame_headers, frames)),
                "audio": stream.read(chunk['size']) if chunk_int(chunk) == codes["audio"] else None
                }
            )

            if chunk_int(chunk) == codes["audio"]:
                chunk = read_chunk(stream)

    def export(self, directory, filename, fmt=("png", "wav"), **kwargs):
        if self.stills:
            for i, still in enumerate(zip(self.stills[0], self.stills[1])):
                still[0].image.export(directory, "still-{}".format(i), fmt=fmt[0], **kwargs)
                with open(os.path.join(directory, "still-{}.txt".format(i)), 'w') as still_header:
                    for datum in still[1].datums:
                        print(repr(datum), file=still_header)

        frame_headers = open(os.path.join(directory, "frame_headers.txt"), 'w')
        image_headers = open(os.path.join(directory, "image_headers.txt"), 'w')

        sound = Sound()

        for i, chunk in enumerate(self.chunks):
            for j, frame in enumerate(chunk["frames"]):
                # Handle the frame headers first
                print(" --- {}-{} ---".format(i, j), file=frame_headers)
                for datum in frame[0].datums:
                    print(repr(datum), file=frame_headers)

                # Now handle the actual frames
                print(" --- {}-{} ---".format(i, j), file=image_headers)
                for datum in frame[1].header.datums:
                    print(repr(datum), file=image_headers)

                if frame[1].image:
                    frame[1].image.export(directory, "{}-{}".format(i, j), fmt=fmt[0], **kwargs)

            if chunk["audio"]: sound.append(chunk["audio"])

        sound.export(directory, "sound", fmt=fmt[1], **kwargs)

        frame_headers.close()
        image_headers.close()

    def __repr__(self):
        return "<Movie: chunks: {}>".format(len(self.chunks))

class Sprite(Object):
    def __init__(self):
        self.frames = []

    def append(self, stream, size):
        start = stream.tell()
        header = Array(stream, bytes=0x24)
        image = Image(stream, dims=header.datums[1], size=size+start-stream.tell(), sprite=True)

        self.frames.append((header, image))

    def export(self, directory, filename, fmt="png", **kwargs):
        frame_headers = open(os.path.join(directory, "frame_headers.txt"), 'w')

        for i, frame in enumerate(self.frames):
            print(" --- {}---".format(i), file=frame_headers)
            for datum in frame[0].datums:
                print(repr(datum), file=frame_headers)

            frame[1].export(directory, str(i), fmt=fmt, **kwargs)

        frame_headers.close()

class Font(Object):
    def __init__(self):
        self.glyphs = []

    def append(self, stream, size):
        start = stream.tell()
        header = Array(stream, bytes=0x22)

        glyph = Image(stream, dims=header.datums[4], size=size+start-stream.tell(), sprite=True)

        self.glyphs.append((header, glyph))

    def export(self, directory, filename, fmt="png", **kwargs):
        frame_headers = open(os.path.join(directory, "frame_headers.txt"), 'w')

        for i, glyph in enumerate(self.glyphs):
            print(" --- {}---".format(i), file=frame_headers)
            for datum in glyph[0].datums:
                print(repr(datum), file=frame_headers)

            glyph[1].export(directory, str(i), fmt=fmt, **kwargs)

        frame_headers.close()

class Sound(Object):
    def __init__(self, stream=None, chunk=None, size=0):
        self.chunks = []
        if size > 0:
            # We want to read a while RIFF if the RIFF size is provided.
            asset_id = chunk["code"]

            start = stream.tell()
            while stream.tell() < start + size:
                assert chunk["code"] == asset_id
                self.chunks.append(stream.read(chunk["size"]))
                if stream.tell() >= start + size:
                    break

                chunk = read_chunk(stream)

                # TODO: Determine a better asset-reading scheme
                if chunk["code"] == "RIFF":
                    stream.seek(stream.tell() - 8)

    def append(self, stream, size=0):
        if isinstance(stream, bytes):
            self.chunks.append(stream)
        else:
            self.chunks.append(stream.read(size))

    def export(self, directory, filename, fmt="wav", **kwargs):
        filename = os.path.join(directory, filename)

        with subprocess.Popen(
                ['ffmpeg', '-y', '-f', 's16le', '-ar', '11.025k', '-ac', '2', '-i', 'pipe:', filename+".{}".format(fmt)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE
        ) as process:
            for chunk in self.chunks:
                process.stdin.write(chunk)

            process.communicate()

        with open(os.path.join(directory, str(len(self.chunks))), 'w') as f:
            f.write(str(len(self.chunks)))

class CxtData(Object):
    def __init__(self, stream):
        riffs, total = read_init(stream)

        self.assets = {}
        self.palette = None
        self.root = None

        ################ Asset headers ###################################
        logging.info("(@0x{:012x}) Reading asset headers...".format(stream.tell()))
        headers = {}

        end = stream.tell() + read_riff(stream)
        chunk = read_chunk(stream)
        while chunk["code"] == 'igod' and stream.tell() < end:
            if Datum(stream).d != ChunkType.HEADER:
                break

            type = Datum(stream)
            if type.d == HeaderType.LINK:
                stream.seek(stream.tell() - 8)
                break
            if type.d == HeaderType.PALETTE:
                assert not self.palette # We cannot have more than 1 palette
                self.palette = stream.read(0x300)
                value_assert(Datum(stream).d, 0x00, "end-of-chunk flag")
            elif type.d == HeaderType.ROOT:
                assert not self.root # We cannot have more than 1 root
                self.root = Array(stream, stop=(DatumType.UINT16, 0x0000))
            elif type.d == HeaderType.ASSET:
                contents = [AssetHeader(stream, size=chunk["size"]-12)]

                if contents[0].type.d == AssetType.STG:
                    contents += contents[0].child

                for header in contents:
                    logging.debug("Found asset header {}".format(header))
                    # TODO: Deal with shared assets.
                    if header.ref and not isinstance(header.ref.d, int):
                        # We have an asset that has further data coming
                        for ref in header.ref.d.id(string=True):
                            headers.update({ref: header})
                    else: # We have an asset that has all necessary data in the header
                        self.assets.update({header.id.d: (header, None)})

                value_assert(Datum(stream).d, 0x00, "end-of-chunk flag")
            else:
                raise TypeError("Unknown header type: {}".format(type))

            chunk = read_chunk(stream)

        ################ First-RIFF assets ###############################
        logging.info("(@0x{:012x}) Reading first-RIFF assets...".format(stream.tell()))

        movie_stills = {}
        while stream.tell() < end:
            if chunk_int(chunk):
                logging.debug("(@0x{:012x}) Accepted chunk {} (0x{:04x} bytes)".format(stream.tell(), chunk["code"], chunk["size"]))
                header = headers[chunk_int(chunk)]
                logging.debug("Linked to header {}".format(header))

                if header.type.d == AssetType.IMG:
                    self.assets.update({header.id.d: (header, Image(stream, size=chunk["size"]))})
                elif header.type.d == AssetType.SND:
                    if not header.id.d in self.assets:
                        self.assets.update({header.id.d: [header, Sound()]})

                    self.assets[header.id.d][1].append(stream, size=chunk["size"])
                elif header.type.d == AssetType.SPR:
                    if header.id.d not in self.assets:
                        self.assets.update({header.id.d: [header, Sprite()]})

                    self.assets[header.id.d][1].append(stream, size=chunk["size"])
                elif header.type.d == AssetType.FON:
                    if header.id.d not in self.assets:
                        self.assets.update({header.id.d: [header, Font()]})

                    self.assets[header.id.d][1].append(stream, size=chunk["size"])
                elif header.type.d == AssetType.MOV:
                    if header.id.d not in movie_stills:
                        movie_stills.update({header.id.d: [[], []]})

                    d = Datum(stream) # read the header
                    if d.d == ChunkType.MOVIE_FRAME:
                        movie_stills[header.id.d][0].append(MovieFrame(stream, size=chunk["size"]))
                    elif d.d == ChunkType.MOVIE_HEADER:
                        movie_stills[header.id.d][1].append(Array(stream, bytes=chunk["size"]-0x04))
                    else:
                        raise ValueError("Unknown header type in movie still area: {}".format(d.d))
                else:
                    raise TypeError("Unhandled asset type found in first chunk: {}".format(header.type.d))

                chunk = read_chunk(stream)

            # TODO: Properly throw away asset links
            while not chunk_int(chunk):
                logging.debug("(@0x{:012x}) Throwing away chunk {} (0x{:04x} bytes)".format(stream.tell(), chunk["code"], chunk["size"]))
                stream.read(chunk["size"])
                if stream.tell() >= end:
                    break

                chunk = read_chunk(stream)

        ################# Chunked assets ##################################
        logging.info("(@0x{:012x}) Reading chunked assets ({} RIFFs)...".format(stream.tell(), riffs-1))

        for i in range(riffs-1):
            start = stream.tell()

            size = read_riff(stream) - 0x24
            end = stream.tell() + size
            logging.debug("(@0x{:012x}) Reading RIFF (0x{:08x} bytes)".format(start, size))

            chunk = read_chunk(stream)
            header = headers.pop(chunk_int(chunk), None)

            if header:
                logging.debug("  >>> {}".format(header))
                if header.type.d == AssetType.MOV:
                    self.assets.update({
                        header.id.d: [header, Movie(stream, chunk, size-0x04, stills=movie_stills.get(header.id.d))]
                    })
                elif header.type.d == AssetType.SND:
                    self.assets.update({header.id.d: [header, Sound(stream, chunk, size-0x04)]})
                else:
                    raise TypeError("Unhandled RIFF asset type: {}".format(header.type.d))

        ################# Junk data #######################################
        self.junk = stream.read()
        if len(self.junk) > 0:
            logging.warning("Found {} bytes at end of file".format(len(self.junk)))

        logging.info("Finished parsing context!")

    def export(self, directory):
        for id, asset in self.assets.items():
            logging.info("Exporting asset {}".format(id))

            path = os.path.join(directory, str(id))
            Path(path).mkdir(parents=True, exist_ok=True)

            with open(os.path.join(directory, str(id), "{}.txt".format(id)), 'w') as header:
                print(repr(asset[0]), file=header)
                for datum in asset[0].data.datums:
                    print(repr(datum), file=header)

            if asset[1]: asset[1].export(path, str(id), palette=self.palette)

        if len(self.junk) > 0:
            with open(os.path.join(directory, "junk"), 'wb') as f:
                f.write(self.junk)


############### SYSTEM PARSER (BOOT.STM)  ################################

class System(Object):
    def __init__(self, stream):
        end = stream.tell() + read_riff(stream)
        chunk = read_chunk(stream)
        header = Array(stream, datums=8)
        unk = Array(stream, datums=2*3) # 401 402 403 (?)

        # Read resource information
        self.resources = []
        type = Datum(stream)
        while type.d == RecordType.RES_NAME:
            name = Datum(stream)

            value_assert(Datum(stream).d, RecordType.RES_ID)
            id = Datum(stream)

            logging.debug("Found resource {} ({})".format(id.d, name.d))
            self.resources.append((id, name))
            type = Datum(stream)

        # Read file headers
        value_assert(type.d, BootRecord.FILES_1, "root signature")
        files = []
        while True: # breaking condition is below
            type = Datum(stream)
            refs = []
            while type.d == RecordType.CXT:
                refs.append(Datum(stream).d)
                type = Datum(stream)

            if type.d == 0x0000:
                break

            if type.d == 0x0003:
                value_assert(Datum(stream).d, 0x0004, "file signature")

                filenum = Datum(stream)
                value_assert(Datum(stream).d, 0x0005, "file signature")
                assert Datum(stream).d == filenum.d

                value_assert(Datum(stream).d, 0x0bb8, "string signature")
                string = Datum(stream)
            else:
                raise ValueError("Received unexpected file signature: {}".format(type.d))

            logging.debug("Found file {} ({})".format(filenum.d, string.d))
            files.append((refs, filenum.d, string.d))

        # Read unknown file information
        value_assert(Datum(stream).d, BootRecord.FILES_2)
        type = Datum(stream)
        while type.d == RecordType.FILE_1:
            value_assert(Datum(stream).d, 0x0009)
            file = Datum(stream)
            value_assert(Datum(stream).d, 0x0004)
            assert file.d == Datum(stream).d

            logging.debug("Referenced file {}".format(file.d))
            type = Datum(stream)

        value_assert(type.d, 0x0000)

        # Now link asset IDs to file names
        value_assert(Datum(stream).d, BootRecord.FILES_3)
        self.files = {}
        type = Datum(stream)

        while type.d == RecordType.FILE_3:
            value_assert(Datum(stream).d, 0x002b)
            id = Datum(stream)
            value_assert(Datum(stream).d, 0x002d)

            filetype = Datum(stream)
            filename = Datum(stream)

            logging.debug("Read file link {} ({})".format(filename.d, id.d))
            self.files.update({id.d: (filename.d, files.pop(0) if filetype.d == 0x0007 else None)})
            type = Datum(stream)

        value_assert(type.d, 0x0000)

        # Link RIFF asset chunks
        value_assert(Datum(stream).d, BootRecord.RIFF)
        self.riffs = {}
        type = Datum(stream)
        while type.d == RecordType.RIFF:
            value_assert(Datum(stream).d, 0x002a)
            asset = Datum(stream)
            value_assert(Datum(stream).d, 0x002b)
            id = Datum(stream)
            value_assert(Datum(stream).d, 0x002c)
            loc = Datum(stream)

            logging.debug("Read RIFF for asset {} ({}:0x{:08x})".format(asset.d, self.files[id.d][0], loc.d))
            self.riffs.update({asset.d: (id.d, loc.d)})
            type = Datum(stream)

        value_assert(type.d, 0x0000)

        # Link resource data
        self.cursors = {}
        value_assert(Datum(stream).d, BootRecord.CURSOR)
        for _ in range(Datum(stream).d, Datum(stream).d): # start and stop
            value_assert(Datum(stream).d, RecordType.CURSOR)
            value_assert(Datum(stream).d, 0x0001)
            id = Datum(stream)
            unk = Datum(stream)
            name = Datum(stream)

            logging.debug("Read cursor {}: {} ({})".format(id.d, name.d, id.d))
            self.cursors.update({id.d: [unk.d, name.d]})

        self.footer = stream.read()

def main(infile):
    logging.basicConfig(level=logging.DEBUG)

    global c
    with open(infile, mode='rb') as f:
        stream = mmap.mmap(f.fileno(), length=0, access=mmap.ACCESS_READ)
        try:
            c = CxtData(stream)
        except:
            logging.error("Exception at 0x{:012x}".format(stream.tell()))
            raise

        c.export(os.path.split(infile)[1])

parser = argparse.ArgumentParser(prog="cxt")
parser.add_argument("input")

args = parser.parse_args()

if __name__ == "__main__":
    main(args.input)
