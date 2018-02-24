# util
from __future__ import absolute_import
import os
import re
import sys
import threading
import errno
import uuid
import time
import tempfile
import logging
import os.path
import gzip
import zlib
from contextlib import contextmanager
from zlib import compress as _compress
try:
    from dpark.portable_hash import portable_hash as _hash
except ImportError:
    import pyximport
    pyximport.install(inplace=True)
    from dpark.portable_hash import portable_hash as _hash

try:
    from cStringIO import StringIO
    BytesIO = StringIO
except ImportError:
    from six import BytesIO, StringIO

try:
    import pwd
    def getuser():
        return pwd.getpwuid(os.getuid()).pw_name
except:
    import getpass
    def getuser():
        return getpass.getuser()

COMPRESS = 'zlib'
def compress(s):
    return _compress(s, 1)

try:
    from dpark.lz4wrapper import compress, decompress
    COMPRESS = 'lz4'
except ImportError:
    try:
        from snappy import compress, decompress
        COMPRESS = 'snappy'
    except ImportError:
        pass

def spawn(target, *args, **kw):
    t = threading.Thread(target=target, name=target.__name__, args=args, kwargs=kw)
    t.daemon = True
    t.start()
    return t

# hash(None) is id(None), different from machines
# http://effbot.org/zone/python-hash.htm
def portable_hash(value):
    return _hash(value)

# similar to itertools.chain.from_iterable, but faster in PyPy
def chain(it):
    for v in it:
        for vv in v:
            yield vv

def izip(*its):
    its = [iter(it) for it in its]
    try:
        while True:
            yield tuple([next(it) for it in its])
    except StopIteration:
        pass

def mkdir_p(path):
    "like `mkdir -p`"
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def memory_str_to_mb(str):
    lower = str.lower()
    if lower[-1].isalpha():
        number, unit = float(lower[:-1]), lower[-1]
    else:
        number, unit = float(lower), 'm'
    scale_factors = {
        'k': 1. / 1024,
        'm': 1,
        'g': 1024,
        't': 1024 * 1024,
    }
    return number * scale_factors[unit]

MIN_REMAIN_RECURSION_LIMIT = 80
def recurion_limit_breaker(f):
    def _(*a, **kw):
        try:
            sys._getframe(sys.getrecursionlimit() - MIN_REMAIN_RECURSION_LIMIT)
        except ValueError:
            return f(*a, **kw)

        def __():
            result = []
            finished = []
            cond = threading.Condition(threading.Lock())
            def _run():
                it = iter(f(*a, **kw))
                with cond:
                    while True:
                        while result:
                            cond.wait()
                        try:
                            result.append(next(it))
                            cond.notify()
                        except StopIteration:
                            break

                    finished.append(1)
                    cond.notify()


            t = spawn(_run)

            with cond:
                while True:
                    while not finished and not result:
                        cond.wait()

                    if result:
                        yield result.pop()
                        cond.notify()

                    if finished:
                        assert not result
                        break

            t.join()

        return __()

    return _

class AbortFileReplacement(Exception):
    pass

@contextmanager
def atomic_file(filename, mode='w+b', bufsize=-1):
    path, name = os.path.split(filename)
    path = path or None
    prefix = '.%s.' % (name,) if name else '.'
    suffix = '.%s.tmp' % (uuid.uuid4().hex,)
    tempname = None
    try:
        try:
            mkdir_p(path)
        except (IOError, OSError):
            time.sleep(1) # there are dir cache in mfs for 1 sec
            mkdir_p(path)

        with tempfile.NamedTemporaryFile(
            mode=mode, suffix=suffix, prefix=prefix,
            dir=path, delete=False) as f:
            tempname = f.name
            yield f

        os.chmod(tempname, 0o644)
        os.rename(tempname, filename)
    except AbortFileReplacement:
        pass
    finally:
        try:
            if tempname:
                os.remove(tempname)
        except OSError:
            pass


RESET = "\033[0m"
BOLD = "\033[1m"
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = [
    "\033[1;%dm" % i for i in range(30, 38)
]

PALLETE = {
    'RESET': RESET,
    'BOLD': BOLD,
    'BLACK': BLACK,
    'RED': RED,
    'GREEN': GREEN,
    'YELLOW': YELLOW,
    'BLUE': BLUE,
    'MAGENTA': MAGENTA,
    'CYAN': CYAN,
    'WHITE': WHITE,
}

COLORS = {
    'WARNING': YELLOW,
    'INFO': WHITE,
    'DEBUG': BLUE,
    'CRITICAL': YELLOW,
    'ERROR': RED
}

FORMAT_PATTERN = re.compile('|'.join('{%s}' % k for k in PALLETE))
def formatter_message(message, use_color = True):
    if use_color:
        return FORMAT_PATTERN.sub(
            lambda m: PALLETE[m.group(0)[1:-1]],
            message
        )

    return FORMAT_PATTERN.sub('', message)

class ColoredFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, use_color = True):
        if fmt:
            fmt = formatter_message(fmt, use_color)

        logging.Formatter.__init__(self, fmt=fmt, datefmt=datefmt)
        self.use_color = use_color

    def format(self, record):
        record = logging.makeLogRecord(record.__dict__)
        levelname = record.levelname
        if self.use_color and levelname in COLORS:
            levelname_color = COLORS[levelname] + levelname + RESET
            record.levelname = levelname_color

        record.msg = formatter_message(record.msg, self.use_color)
        return logging.Formatter.format(self, record)

USE_UTF8 = getattr(sys.stderr, 'encoding', None) == 'UTF-8'

ASCII_BAR = ('[ ', ' ]', '#', '-', '-\\|/-\\|')
UNICODE_BAR = (u'[ ', u' ]', u'\u2589', u'-',
    u'-\u258F\u258E\u258D\u258C\u258B\u258A')

def make_progress_bar(ratio, size=14):
    if USE_UTF8:
        L, R, B, E, F = UNICODE_BAR
    else:
        L, R, B, E, F = ASCII_BAR

    if size > 4:
        n = size - 4
        with_border = True
    else:
        n = size
        with_border = False

    p = n * ratio
    blocks = int(p)
    if p > blocks:
        frac = int((p - blocks) * 7)
        blanks = n - blocks - 1
        C = F[frac]
    else:
        blanks = n - blocks
        C = ''

    if with_border:
        return ''.join([L, B * blocks, C, E * blanks, R])
    else:
        return ''.join([B * blocks, C, E * blanks])

def init_dpark_logger(log_level, use_color=None):
    log_format = '{GREEN}%(asctime)-15s{RESET}' \
        ' [%(levelname)s] [%(name)-9s] %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    logger = get_logger('dpark')
    logger.propagate = False

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(ColoredFormatter(log_format, datefmt, use_color))

    logger.addHandler(handler)
    logger.setLevel(max(log_level, logger.level))


def get_logger(name):
    """ Always use logging.Logger class.

    The user code may change the loggerClass (e.g. pyinotify),
    and will cause exception when format log message.
    """
    old_class = logging.getLoggerClass()
    logging.setLoggerClass(logging.Logger)
    logger = logging.getLogger(name)
    logging.setLoggerClass(old_class)
    return logger


def default_crc32c_fn(value):
    if not default_crc32c_fn.fn:
        import crcmod
        default_crc32c_fn.fn = crcmod.predefined.mkPredefinedCrcFun('crc-32c')
    return default_crc32c_fn.fn(value)

default_crc32c_fn.fn = None


def masked_crc32c(value, crc32c_fn=default_crc32c_fn):
    crc = crc32c_fn(value)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def gzip_find_block(f, pos):
    f.seek(pos)
    block = f.read(32 * 1024)
    if len(block) < 4:
        f.seek(0, 2)
        return f.tell()  # EOF
    ENDING = b'\x00\x00\xff\xff'
    while True:
        p = block.find(ENDING)
        while p < 0:
            pos += max(len(block) - 3, 0)
            block = block[-3:] + f.read(32 << 10)
            if len(block) < 4:
                return pos + 3  # EOF
            p = block.find(ENDING)
        pos += p + 4
        block = block[p + 4:]
        if len(block) < 4096:
            block += f.read(4096)
            if not block:
                return pos  # EOF
        try:
            dz = zlib.decompressobj(-zlib.MAX_WBITS)
            if dz.decompress(block) and len(dz.unused_data) <= 8:
                return pos  # FOUND
        except Exception as e:
            pass

logger = get_logger(__name__)


def gzip_decompressed_fh(f, path, split, splitSize):
    if split.index == 0:
        zf = gzip.GzipFile(mode='rb', fileobj=f)
        if hasattr(zf, '_buffer'):
            zf._buffer.raw._read_gzip_header()
        else:
            zf._read_gzip_header()
        zf.close()
        start = f.tell()
    else:
        start = gzip_find_block(f, split.index * splitSize)
        if start >= split.index * splitSize + splitSize:
            return
    end = gzip_find_block(f, split.index * splitSize + splitSize)
    f.seek(start)
    f.length = end
    dz = zlib.decompressobj(-zlib.MAX_WBITS)
    while start < end:
        d = f.read(min(64 << 10, end - start))
        start += len(d)
        if not d:
            break
        try:
            io = BytesIO(dz.decompress(d))
        except Exception as e:
            logger.error("failed to decompress file: %s", path)
            old = start
            start = gzip_find_block(f, start)
            f.seek(start)
            logger.error("drop corrupted block (%d bytes) in %s",
                         start - old + len(d), path)
            continue
        yield io
        if len(dz.unused_data) > 8:
            f.seek(-len(dz.unused_data) + 8, 1)
            zf = gzip.GzipFile(mode='r', fileobj=f)
            if hasattr(zf, '_buffer'):
                zf._buffer.raw._read_gzip_header()
            else:
                zf._read_gzip_header()
            zf.close()
            dz = zlib.decompressobj(-zlib.MAX_WBITS)
            start -= f.tell()


src_dir = os.path.dirname(os.path.abspath(__file__))
STACK_FILE_NAME = 0
STACK_LINE_NUM = 1
STACK_FUNC_NAME = 2


def get_user_call_site():
    import traceback
    stack = traceback.extract_stack(sys._getframe())
    for i in range(1, len(stack)):
        callee_path = stack[i][STACK_FILE_NAME]
        if src_dir == os.path.dirname(os.path.abspath(callee_path)):
            caller_path = stack[i-1][STACK_FILE_NAME]
            caller_lineno = stack[i-1][STACK_LINE_NUM]
            dpark_func_name = stack[i][STACK_FUNC_NAME]
            user_call_site = '%s:%d ' % (caller_path, caller_lineno)
            return dpark_func_name, user_call_site
    return "<func>", " <root>"


class Scope(object):
    def __init__(self):
        fn, pos = get_user_call_site()
        self.dpark_func_name = fn
        self.call_site = "@".join([fn, pos])

