#!/usr/bin/env python3
"""Serve this map bundle locally.  Standard library only -- nothing to install.

    python3 serve.py                    # http://127.0.0.1:8000/
    python3 serve.py 8080
    python3 serve.py 8080 --host 0.0.0.0    # let the rest of the LAN in
    python3 serve.py --open                 # and open a browser

**`python -m http.server` does not work here.**  It has no `Range` support, so
`pmtiles.js` asks for 16 KB of the archive and gets the whole file back for every
single tile -- the page loads, the map stays blank, and nothing in the console
says why.  Supplying that one missing feature is the entire reason this file
exists; everything else is `http.server`'s own static handling.

The same requirement applies to whatever you eventually upload the bundle to:
the host must answer `Range` with `206`, and must serve the `.pmtiles`
untransformed (a CDN that gzips it breaks the byte offsets the format is built
on).
"""
import argparse
import os
import re
import socket
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header, size):
    """`Range` header -> (first, last) inclusive, or None if it does not apply.

    Returns the string "unsatisfiable" when the range is well-formed but outside
    the file, which is a 416; a malformed header returns None, and RFC 9110 says
    to ignore it and serve the whole thing.
    """
    match = _RANGE.match((header or "").strip())
    if not match:
        return None                 # multi-range or junk: fall back to a 200
    start, end = match.group(1), match.group(2)
    if start == "" and end == "":
        return None
    if start == "":                 # bytes=-500 -> the last 500 bytes
        length = int(end)
        if length == 0:
            return "unsatisfiable"
        first = max(0, size - length)
        last = size - 1
    else:
        first = int(start)
        last = size - 1 if end == "" else min(int(end), size - 1)
    if first >= size or first > last:
        return "unsatisfiable"
    return first, last


class RangeHandler(SimpleHTTPRequestHandler):
    # HTTP/1.1 for keep-alive: one viewport pulls dozens of tiles, and a fresh
    # TCP connection per tile is the difference between instant and sluggish.
    protocol_version = "HTTP/1.1"

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".pmtiles": "application/octet-stream",
        ".webp": "image/webp",
        ".js": "text/javascript",
        ".json": "application/json",
    }

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        # So the bundle can also act as the tile host for a page served from
        # somewhere else, which is the usual way to try a layout before
        # uploading anything.
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def send_head(self):
        self._remaining = None
        header = self.headers.get("Range")
        path = self.translate_path(self.path)
        if header is None or os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            size = os.fstat(f.fileno()).st_size
            span = parse_range(header, size)
            if span is None:
                f.close()
                return super().send_head()
            if span == "unsatisfiable":
                f.close()
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            first, last = span
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.send_header("Content-Length", str(last - first + 1))
            self.send_header("Last-Modified",
                             self.date_time_string(os.fstat(f.fileno()).st_mtime))
            self.end_headers()
            f.seek(first)
            self._remaining = last - first + 1
            return f
        except Exception:
            f.close()
            raise

    def copyfile(self, source, outputfile):
        """Copy only the requested slice.

        The base class streams to EOF, which for a 700 MB archive would send the
        whole thing on every tile request -- exactly the `http.server` failure
        this handler exists to avoid.
        """
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def log_message(self, fmt, *args):
        if not QUIET:
            super().log_message(fmt, *args)


QUIET = False


def lan_address():
    """The address this machine is reachable at, without any DNS lookup."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))     # never sends a packet
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def serve(directory, port=8000, host="127.0.0.1", open_browser=False, quiet=False):
    global QUIET
    QUIET = quiet

    def factory(*args, **kwargs):
        return RangeHandler(*args, directory=directory, **kwargs)

    httpd = ThreadingHTTPServer((host, port), factory)
    port = httpd.server_address[1]              # in case port was 0
    shown = "127.0.0.1" if host in ("", "0.0.0.0") else host
    url = f"http://{shown}:{port}/"
    print(f"serving {directory}\n  {url}")
    if host in ("", "0.0.0.0"):
        lan = lan_address()
        if lan:
            print(f"  http://{lan}:{port}/   (from other machines on the LAN)")
    print("ctrl-c to stop")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to accept connections from the network")
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="what to serve (default: this file's directory)")
    ap.add_argument("--open", action="store_true", dest="open_browser")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    args = ap.parse_args(argv)
    serve(args.dir, args.port, args.host, args.open_browser, args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
