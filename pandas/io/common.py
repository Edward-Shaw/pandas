"""Common IO api utilities"""

import sys
import os
import zipfile
from contextlib import contextmanager, closing

from pandas.compat import StringIO, string_types, BytesIO
from pandas import compat


if compat.PY3:
    from urllib.request import urlopen, pathname2url
    _urlopen = urlopen
    from urllib.parse import urlparse as parse_url
    import urllib.parse as compat_parse
    from urllib.parse import (uses_relative, uses_netloc, uses_params,
                              urlencode, urljoin)
    from urllib.error import URLError
    from http.client import HTTPException
else:
    from urllib2 import urlopen as _urlopen
    from urllib import urlencode, pathname2url
    from urlparse import urlparse as parse_url
    from urlparse import uses_relative, uses_netloc, uses_params, urljoin
    from urllib2 import URLError
    from httplib import HTTPException
    from contextlib import contextmanager, closing
    from functools import wraps

    # @wraps(_urlopen)
    @contextmanager
    def urlopen(*args, **kwargs):
        with closing(_urlopen(*args, **kwargs)) as f:
            yield f


_VALID_URLS = set(uses_relative + uses_netloc + uses_params)
_VALID_URLS.discard('')


class PerformanceWarning(Warning):
    pass


class DtypeWarning(Warning):
    pass


try:
    from boto.s3 import key
    class BotoFileLikeReader(key.Key):
        """boto Key modified to be more file-like

        This modification of the boto Key will read through a supplied
        S3 key once, then stop. The unmodified boto Key object will repeatedly
        cycle through a file in S3: after reaching the end of the file,
        boto will close the file. Then the next call to `read` or `next` will
        re-open the file and start reading from the beginning.

        Also adds a `readline` function which will split the returned
        values by the `\n` character.
        """
        def __init__(self, *args, **kwargs):
            encoding = kwargs.pop("encoding", None)  # Python 2 compat
            super(BotoFileLikeReader, self).__init__(*args, **kwargs)
            self.finished_read = False  # Add a flag to mark the end of the read.
            self.buffer = ""
            self.lines = []
            if encoding is None and compat.PY3:
                encoding = "utf-8"
            self.encoding = encoding
            self.lines = []

        def next(self):
            return self.readline()

        __next__ = next

        def read(self, *args, **kwargs):
            if self.finished_read:
                return b'' if compat.PY3 else ''
            return super(BotoFileLikeReader, self).read(*args, **kwargs)

        def close(self, *args, **kwargs):
            self.finished_read = True
            return super(BotoFileLikeReader, self).close(*args, **kwargs)

        def seekable(self):
            """Needed for reading by bz2"""
            return False

        def readline(self):
            """Split the contents of the Key by '\n' characters."""
            if self.lines:
                retval = self.lines[0]
                self.lines = self.lines[1:]
                return retval
            if self.finished_read:
                if self.buffer:
                    retval, self.buffer = self.buffer, ""
                    return retval
                else:
                    raise StopIteration

            if self.encoding:
                self.buffer = "{}{}".format(self.buffer, self.read(8192).decode(self.encoding))
            else:
                self.buffer = "{}{}".format(self.buffer, self.read(8192))

            split_buffer = self.buffer.split("\n")
            self.lines.extend(split_buffer[:-1])
            self.buffer = split_buffer[-1]

            return self.readline()
except ImportError:
    # boto is only needed for reading from S3.
    pass


def _is_url(url):
    """Check to see if a URL has a valid protocol.

    Parameters
    ----------
    url : str or unicode

    Returns
    -------
    isurl : bool
        If `url` has a valid protocol return True otherwise False.
    """
    try:
        return parse_url(url).scheme in _VALID_URLS
    except:
        return False


def _is_s3_url(url):
    """Check for an s3, s3n, or s3a url"""
    try:
        return parse_url(url).scheme in ['s3', 's3n', 's3a']
    except:
        return False


def maybe_read_encoded_stream(reader, encoding=None, compression=None):
    """read an encoded stream from the reader and transform the bytes to
    unicode if required based on the encoding

        Parameters
        ----------
        reader : a streamable file-like object
        encoding : optional, the encoding to attempt to read

        Returns
        -------
        a tuple of (a stream of decoded bytes, the encoding which was used)

    """

    if compat.PY3 or encoding is not None:  # pragma: no cover
        if encoding:
            errors = 'strict'
        else:
            errors = 'replace'
            encoding = 'utf-8'

        if compression == 'gzip':
            reader = BytesIO(reader.read())
        else:
            reader = StringIO(reader.read().decode(encoding, errors))
    else:
        if compression == 'gzip':
            reader = BytesIO(reader.read())
        encoding = None
    return reader, encoding


def _expand_user(filepath_or_buffer):
    """Return the argument with an initial component of ~ or ~user
       replaced by that user's home directory.

    Parameters
    ----------
    filepath_or_buffer : object to be converted if possible

    Returns
    -------
    expanded_filepath_or_buffer : an expanded filepath or the
                                  input if not expandable
    """
    if isinstance(filepath_or_buffer, string_types):
        return os.path.expanduser(filepath_or_buffer)
    return filepath_or_buffer


def get_filepath_or_buffer(filepath_or_buffer, encoding=None,
                           compression=None):
    """
    If the filepath_or_buffer is a url, translate and return the buffer
    passthru otherwise.

    Parameters
    ----------
    filepath_or_buffer : a url, filepath, or buffer
    encoding : the encoding to use to decode py3 bytes, default is 'utf-8'

    Returns
    -------
    a filepath_or_buffer, the encoding, the compression
    """

    if _is_url(filepath_or_buffer):
        req = _urlopen(str(filepath_or_buffer))
        if compression == 'infer':
            content_encoding = req.headers.get('Content-Encoding', None)
            if content_encoding == 'gzip':
                compression = 'gzip'
            else:
                compression = None
        # cat on the compression to the tuple returned by the function
        to_return = list(maybe_read_encoded_stream(req, encoding, compression)) + \
                    [compression]
        return tuple(to_return)

    if _is_s3_url(filepath_or_buffer):
        try:
            import boto
        except:
            raise ImportError("boto is required to handle s3 files")
        # Assuming AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
        # are environment variables
        parsed_url = parse_url(filepath_or_buffer)

        try:
            conn = boto.connect_s3()
        except boto.exception.NoAuthHandlerFound:
            conn = boto.connect_s3(anon=True)

        b = conn.get_bucket(parsed_url.netloc, validate=False)
        if compat.PY2 and (compression == 'gzip' or
                           (compression == 'infer' and
                            filepath_or_buffer.endswith(".gz"))):
            k = boto.s3.key.Key(b, parsed_url.path)
            filepath_or_buffer = BytesIO(k.get_contents_as_string(
                encoding=encoding))
        else:
            k = BotoFileLikeReader(b, parsed_url.path, encoding=encoding)
            k.open('r')  # Expose read errors immediately
            filepath_or_buffer = k
        return filepath_or_buffer, None, compression

    return _expand_user(filepath_or_buffer), None, compression


def file_path_to_url(path):
    """
    converts an absolute native path to a FILE URL.

    Parameters
    ----------
    path : a path in native format

    Returns
    -------
    a valid FILE URL
    """
    return urljoin('file:', pathname2url(path))


# ZipFile is not a context manager for <= 2.6
# must be tuple index here since 2.6 doesn't use namedtuple for version_info
if sys.version_info[1] <= 6:
    @contextmanager
    def ZipFile(*args, **kwargs):
        with closing(zipfile.ZipFile(*args, **kwargs)) as zf:
            yield zf
else:
    ZipFile = zipfile.ZipFile
