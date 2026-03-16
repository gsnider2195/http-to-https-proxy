import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import requests
from werkzeug.datastructures import MultiDict


LISTEN_PORT = 5000
REQUESTS_PROXIES = {"http": None, "https": None}


class ProxyHTTPRequestHandler(ThreadingMixIn, BaseHTTPRequestHandler):

    def _log_debug(self, format, *args):
        if "--debug" not in sys.argv:
            return

        print("DEBUG: %s" % (format % args), file=sys.stderr)

    def _send_header(self, format, *args):
        self.wfile.write(((format % args) + "\r\n").encode("latin-1", "strict"))

    def handle_one_request(self):
        """Handle a single HTTP request and proxy it to the HTTPS version of the site."""
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                # An error code has been sent, just exit
                return

            self._log_debug("received request for %s: %s", self.command, self.path)
            self._log_debug("request headers:\n%s", self.headers)

            length = int(self.headers.get("content-length", 0))
            if self.command.lower() == "post" and length > 0:
                body = self.rfile.read(length)
            else:
                body = None

            url = self.path.replace("http://", "https://")
            headers = MultiDict([(k, v.replace("http://", "https://")) for k, v in self.headers.items()])
 
            req = requests.Request(self.command, url, headers=headers).prepare()
            if body:
                req.body = body
            s = requests.Session()
            chunked = self.request_version == "HTTP/1.1" # Try to chunk requests if client supports HTTP/1.1
            resp = s.send(req, proxies=REQUESTS_PROXIES, allow_redirects=False, stream=chunked)

            if resp.headers.get("content-length", "0") == "0":
                chunked = False

            self._log_debug("received response from: %s", url)
            self._log_debug("response headers: %s", resp.headers)

            # Send first line of HTTP Headers
            http_version = "%s.%s" % (str(resp.raw.version)[0], str(resp.raw.version)[1])
            self._send_header("HTTP/%s %s %s", http_version, resp.status_code, resp.reason)

            # Replace https:// in content with http://
            # Some sites use https:&#x2F;&#x2F; instead of https://
            if resp.headers.get("content-type", "").lower().startswith("text"):
                content = b""
                if chunked:
                    chunked = False  # Do not chunk text because we need access to the full content to replace https strings
                    for chunk in resp.iter_content(chunk_size=None):
                        content += chunk
                else:
                    content = resp.content

                # Decode the content and replace strings
                encoding = "utf-8"
                if "charset=" in resp.headers.get("content-type", ""):
                    encoding = resp.headers.get("content-type").split("charset=")[1]
                content = content.decode(encoding, "replace")
                content = content.replace("https://", "http://")
                content = content.replace("https:&#x2F;&#x2F;", "http:&#x2F;&#x2F;")

                # Re-encode the content before returning to the client
                content = content.encode(encoding)

            elif not chunked:
                content = resp.content

            # Send the rest of the headers from proxied request
            # Fix some of the headers that are no longer relevant or are incorrect
            # Replace https with http in header values before sending back to client
            for k, v in resp.headers.items():
                if k.lower() == "content-encoding":
                    continue
                if k.lower() == "transfer-encoding" and "chunked" in v.lower() and not chunked:
                    self._send_header("Content-Length: %s", len(content))
                elif k.lower() == "content-length" and not chunked:
                    self._send_header("Content-Length: %s", len(content))
                else:
                    v = v.replace("https://", "http://")
                    self._send_header("%s: %s", k, v)

            if chunked:
                if "transfer-encoding" not in resp.headers:
                    self._send_header("Transfer-Encoding: chunked")

                self._log_debug("returning chunked content")
                self._send_header("")

                for chunk in resp.iter_content(chunk_size=None):
                    self._send_header("%X", len(chunk))
                    self.wfile.write(chunk)
                    self._send_header("")
                    self.wfile.flush()

                self._send_header("0\r\n")

            else:
                self._log_debug("return content length: %s", len(content))
                if resp.headers.get("content-type", "").lower().startswith("text") and not chunked:
                    self._log_debug(content.replace(b"%", b"%%"))

                # Send the content
                self._send_header("")
                self.wfile.write(content)

            self.wfile.flush() #actually send the response if not already done.

            # Log the response information
            self.log_message('"%s" %s %s', self.requestline.strip(), resp.status_code, resp.headers.get("content-length", "-"))

        except TimeoutError as e:
            # a read or a write timed out.  Discard this connection
            self.log_error("Request timed out: %r", e)
            self.close_connection = True
            return 


server_address = ("", LISTEN_PORT)
httpd = HTTPServer(server_address, ProxyHTTPRequestHandler)
print("Starting server on port %s" % (LISTEN_PORT), file=sys.stderr)
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print(file=sys.stderr)
    sys.exit()

