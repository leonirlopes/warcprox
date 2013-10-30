#!/usr/bin/python
# vim:set sw=4 et:
#

import BaseHTTPServer, SocketServer
import socket
import urlparse
import OpenSSL
import ssl
import logging
import sys
from hanzo import warctools
import hashlib
from datetime import datetime
import Queue
import threading
import os
import argparse
import random
import httplib
import re
import signal
import time
import tempfile
import base64

class CertificateAuthority(object):

    def __init__(self, ca_file='warcprox-ca.pem', certs_dir='./warcprox-ca'):
        self.ca_file = ca_file
        self.certs_dir = certs_dir

        if not os.path.exists(ca_file):
            self._generate_ca()
        else:
            self._read_ca(ca_file)

        if not os.path.exists(certs_dir):
            logging.info("directory for generated certs {} doesn't exist, creating it".format(certs_dir))
            os.mkdir(certs_dir)


    def _generate_ca(self):
        # Generate key
        self.key = OpenSSL.crypto.PKey()
        self.key.generate_key(OpenSSL.crypto.TYPE_RSA, 2048)

        # Generate certificate
        self.cert = OpenSSL.crypto.X509()
        self.cert.set_version(3)
        # avoid sec_error_reused_issuer_and_serial
        self.cert.set_serial_number(random.randint(0,2**64-1))
        self.cert.get_subject().CN = 'warcprox certificate authority on {}'.format(socket.gethostname())
        self.cert.gmtime_adj_notBefore(0)                # now
        self.cert.gmtime_adj_notAfter(100*365*24*60*60)  # 100 yrs in future
        self.cert.set_issuer(self.cert.get_subject())
        self.cert.set_pubkey(self.key)
        self.cert.add_extensions([
            OpenSSL.crypto.X509Extension("basicConstraints", True, "CA:TRUE, pathlen:0"),
            OpenSSL.crypto.X509Extension("keyUsage", True, "keyCertSign, cRLSign"),
            OpenSSL.crypto.X509Extension("subjectKeyIdentifier", False, "hash", subject=self.cert),
            ])
        self.cert.sign(self.key, "sha1")

        with open(self.ca_file, 'wb+') as f:
            f.write(OpenSSL.crypto.dump_privatekey(OpenSSL.SSL.FILETYPE_PEM, self.key))
            f.write(OpenSSL.crypto.dump_certificate(OpenSSL.SSL.FILETYPE_PEM, self.cert))

        logging.info('generated CA key+cert and wrote to {}'.format(self.ca_file))


    def _read_ca(self, filename):
        self.cert = OpenSSL.crypto.load_certificate(OpenSSL.SSL.FILETYPE_PEM, open(filename).read())
        self.key = OpenSSL.crypto.load_privatekey(OpenSSL.SSL.FILETYPE_PEM, open(filename).read())
        logging.info('read CA key+cert from {}'.format(self.ca_file))

    def __getitem__(self, cn):
        cnp = os.path.sep.join([self.certs_dir, '%s.pem' % cn])
        if not os.path.exists(cnp):
            # create certificate
            key = OpenSSL.crypto.PKey()
            key.generate_key(OpenSSL.crypto.TYPE_RSA, 2048)

            # Generate CSR
            req = OpenSSL.crypto.X509Req()
            req.get_subject().CN = cn
            req.set_pubkey(key)
            req.sign(key, 'sha1')

            # Sign CSR
            cert = OpenSSL.crypto.X509()
            cert.set_subject(req.get_subject())
            cert.set_serial_number(random.randint(0,2**64-1))
            cert.gmtime_adj_notBefore(0)
            cert.gmtime_adj_notAfter(10*365*24*60*60)
            cert.set_issuer(self.cert.get_subject())
            cert.set_pubkey(req.get_pubkey())
            cert.sign(self.key, 'sha1')

            with open(cnp, 'wb+') as f:
                f.write(OpenSSL.crypto.dump_privatekey(OpenSSL.SSL.FILETYPE_PEM, key))
                f.write(OpenSSL.crypto.dump_certificate(OpenSSL.SSL.FILETYPE_PEM, cert))

            logging.info('wrote generated key+cert to {}'.format(cnp))

        return cnp


class UnsupportedSchemeException(Exception):
    pass


# This class intercepts the raw bytes, so it's the easiest place to hook in to
# send the raw bytes on to the proxy destination.
class ProxyingRecorder:

    def __init__(self, fp, proxy_dest):
        self.fp = fp
        # "The file has no name, and will cease to exist when it is closed."
        self.tempfile = tempfile.SpooledTemporaryFile(max_size=512*1024)
        self.block_sha1 = hashlib.sha1()
        self.payload_offset = None
        self.payload_sha1 = None
        self.proxy_dest = proxy_dest
        self._prev_hunk_last_two_bytes = ''
        self.len = 0

    def _update(self, hunk):
        if self.payload_sha1 is None:
            # convoluted handling of two newlines crossing hunks
            # XXX write tests for this
            if self._prev_hunk_last_two_bytes.endswith('\n'):
                if hunk.startswith('\n'):
                    self.payload_sha1 = hashlib.sha1()
                    self.payload_sha1.update(hunk[1:])
                    self.payload_offset = self.len + 1
                elif hunk.startswith('\r\n'):
                    self.payload_sha1 = hashlib.sha1()
                    self.payload_sha1.update(hunk[2:])
                    self.payload_offset = self.len + 2
            elif self._prev_hunk_last_two_bytes == '\n\r':
                if hunk.startswith('\n'):
                    self.payload_sha1 = hashlib.sha1()
                    self.payload_sha1.update(hunk[1:])
                    self.payload_offset = self.len + 1
            else:
                m = re.search(r'\n\r?\n', hunk)
                if m is not None:
                    self.payload_sha1 = hashlib.sha1()
                    self.payload_sha1.update(hunk[m.end():])
                    self.payload_offset = self.len + m.end()

            # if we still haven't found start of payload hold on to these bytes
            if self.payload_sha1 is None:
                self._prev_hunk_last_two_bytes = hunk[-2:]
        else:
            self.payload_sha1.update(hunk)

        self.block_sha1.update(hunk)

        self.tempfile.write(hunk)
        self.proxy_dest.sendall(hunk)
        self.len += len(hunk)


    def read(self, size=-1):
        hunk = self.fp.read(size=size)
        self._update(hunk)
        return hunk

    def readline(self, size=-1):
        # XXX depends on implementation details of self.fp.readline(), in
        # particular that it doesn't call self.fp.read()
        hunk = self.fp.readline(size=size)
        self._update(hunk)
        return hunk

    def close(self):
        return self.fp.close()

    def __len__(self):
        return self.len


class ProxyingRecordingHTTPResponse(httplib.HTTPResponse):

    def __init__(self, sock, debuglevel=0, strict=0, method=None, buffering=False, proxy_dest=None):
        httplib.HTTPResponse.__init__(self, sock, debuglevel=debuglevel, strict=strict, method=method, buffering=buffering)

        # Keep around extra reference to self.fp because HTTPResponse sets
        # self.fp=None after it finishes reading, but we still need it
        self.recorder = ProxyingRecorder(self.fp, proxy_dest)
        self.fp = self.recorder


class WarcProxyHandler(BaseHTTPServer.BaseHTTPRequestHandler):

    def __init__(self, request, client_address, server):
        self.is_connect = False
        BaseHTTPServer.BaseHTTPRequestHandler.__init__(self, request, client_address, server)

    def _connect_to_host(self):
        # Get hostname and port to connect to
        if self.is_connect:
            self.hostname, self.port = self.path.split(':')
        else:
            self.url = self.path
            u = urlparse.urlparse(self.url)
            if u.scheme != 'http':
                raise UnsupportedSchemeException('Unknown scheme %s' % repr(u.scheme))
            self.hostname = u.hostname
            self.port = u.port or 80
            self.path = urlparse.urlunparse(
                urlparse.ParseResult(
                    scheme='',
                    netloc='',
                    params=u.params,
                    path=u.path or '/',
                    query=u.query,
                    fragment=u.fragment
                )
            )

        # Connect to destination
        self._proxy_sock = socket.socket()
        self._proxy_sock.settimeout(10)
        self._proxy_sock.connect((self.hostname, int(self.port)))

        # Wrap socket if SSL is required
        if self.is_connect:
            self._proxy_sock = ssl.wrap_socket(self._proxy_sock)


    def _transition_to_ssl(self):
        self.request = self.connection = ssl.wrap_socket(self.connection, 
                server_side=True, certfile=self.server.ca[self.hostname])


    def do_CONNECT(self):
        self.is_connect = True
        try:
            # Connect to destination first
            self._connect_to_host()

            # If successful, let's do this!
            self.send_response(200, 'Connection established')
            self.end_headers()
            self._transition_to_ssl()
        except Exception as e:
            self.send_error(500, str(e))
            return

        # Reload!
        self.setup()
        self.handle_one_request()


    def _construct_tunneled_url(self):
        if int(self.port) == 443:
            netloc = self.hostname
        else:
            netloc = '{}:{}'.format(self.hostname, self.port)

        result = urlparse.urlunparse(
            urlparse.ParseResult(
                scheme='https',
                netloc=netloc,
                params='',
                path=self.path,
                query='',
                fragment=''
            )
        )

        return result


    def do_COMMAND(self):

        if not self.is_connect:
            try:
                # Connect to destination
                self._connect_to_host()
                assert self.url
            except Exception as e:
                self.send_error(500, str(e))
                return
        else:
            self.url = self._construct_tunneled_url()

        # Build request
        req = '%s %s %s\r\n' % (self.command, self.path, self.request_version)
        
        # Add headers to the request
        req += '%s\r\n' % self.headers

        # Append message body if present to the request
        if 'Content-Length' in self.headers:
            req += self.rfile.read(int(self.headers['Content-Length']))
            
        # Send it down the pipe!
        self._proxy_sock.sendall(req)

        # We want HTTPResponse's smarts about http and handling of
        # non-compliant servers. But HTTPResponse.read() doesn't return the raw
        # bytes read from the server, it unchunks them if they're chunked, and
        # might do other stuff. We want to send the raw bytes back to the
        # client. So we ignore the values returned by h.read() below. Instead
        # the ProxyingRecordingHTTPResponse takes care of sending the raw bytes
        # to the proxy client.
        
        # Proxy and record the response
        h = ProxyingRecordingHTTPResponse(self._proxy_sock, proxy_dest=self.connection)
        h.begin()
        
        buf = h.read(8192) 
        while buf != '':
            buf = h.read(8192) 

        remote_ip = self._proxy_sock.getpeername()[0]

        # Let's close off the remote end
        h.close()
        self._proxy_sock.close()

        self.server.recordset_q.create_and_queue(self.url, req, h.recorder, remote_ip)


    def __getattr__(self, item):
        if item.startswith('do_'):
            return self.do_COMMAND

    def log_error(self, fmt, *args):
        logging.error("{0} - - [{1}] {2}".format(self.address_string(), 
            self.log_date_time_string(), fmt % args))

    def log_message(self, fmt, *args):
        logging.info("{0} - - [{1}] {2}".format(self.address_string(), 
            self.log_date_time_string(), fmt % args))


class WarcProxy(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):

    def __init__(self, server_address, req_handler_class=WarcProxyHandler, 
            bind_and_activate=True, ca_file='./warcprox-ca.pem', 
            certs_dir='./warcprox-ca', recordset_q=None):
        BaseHTTPServer.HTTPServer.__init__(self, server_address, req_handler_class, bind_and_activate)
        self.ca = CertificateAuthority(ca_file, certs_dir)
        self.recordset_q = recordset_q

    def server_activate(self):
        BaseHTTPServer.HTTPServer.server_activate(self)
        logging.info('listening on {0}:{1}'.format(self.server_address[0], self.server_address[1]))

    def server_close(self):
        logging.info('shutting down')
        BaseHTTPServer.HTTPServer.server_close(self)

class DedupDb:

    def __init__(self):
        # XXX in memory for the moment
        self.db = {}


    def warc_record_written(self, record, warcfile, offset):
        warc_type = record.get_header(warctools.WarcRecord.TYPE)
        if warc_type != warctools.WarcRecord.RESPONSE:
            return

        payload_digest = record.get_header(warctools.WarcRecord.PAYLOAD_DIGEST)
        if payload_digest is None:
            return

        record_id = record.get_header(warctools.WarcRecord.ID)
        url = record.get_header(warctools.WarcRecord.URL)
        date = record.get_header(warctools.WarcRecord.DATE)

        self.db[payload_digest] = {'i':record_id, 'u':url, 'd':date}


    def lookup(self, key):
        if key in self.db:
            return self.db[key]
        else:
            return None


# Each item in the queue is a tuple of warc records, which should be written
# consecutively in the same warc.
class WarcRecordsetQueue(Queue.Queue):

    def __init__(self, base32=False, dedup_db=None):
        Queue.Queue.__init__(self)
        self.base32 = base32


    def create_and_queue(self, url, request_data, response_recorder, remote_ip):
        warc_date = warctools.warc.warc_datetime_str(datetime.now())

        if dedup_db is not None and response_recorder.payload_sha1 is not None:
            key = 'sha1:{}'.format(self.digest_str(response_recorder.payload_sha1))
            dedup_info = dedup_db.lookup(key)

        if dedup_info is not None:
            # revisit record
            response_recorder.tempfile.seek(0)
            if response_recorder.payload_offset is not None:
                response_header_block = response_recorder.tempfile.read(response_recorder.payload_offset)
            else:
                response_header_block = response_recorder.tempfile.read()

            principal_record, principal_record_id = self.make_record(url=url,
                    warc_date=warc_date, data=response_header_block,
                    warc_type=warctools.WarcRecord.REVISIT,
                    refers_to=dedup_info['i'],
                    refers_to_target_uri=dedup_info['u'],
                    refers_to_date=dedup_info['d'],
                    profile=warctools.WarcRecord.PROFILE_IDENTICAL_PAYLOAD_DIGEST,
                    content_type=warctools.WarcRecord.HTTP_RESPONSE_MIMETYPE,
                    remote_ip=remote_ip)

        else:
            # response record
            principal_record, principal_record_id = self.make_record(url=url,
                    warc_date=warc_date, recorder=response_recorder, 
                    warc_type=warctools.WarcRecord.RESPONSE,
                    content_type=warctools.WarcRecord.HTTP_RESPONSE_MIMETYPE,
                    remote_ip=remote_ip)

        request_record, request_record_id = self.make_record(url=url,
                warc_date=warc_date, data=request_data, 
                warc_type=warctools.WarcRecord.REQUEST, 
                content_type=warctools.WarcRecord.HTTP_REQUEST_MIMETYPE,
                concurrent_to=principal_record_id)

        record_group = (principal_record, request_record)
        self.put(record_group)


    def digest_str(self, hash_obj):
        if self.base32:
            return base64.b32encode(hash_obj.digest())
        else:
            return hash_obj.hexdigest()


    def make_record(self, url, warc_date=None, recorder=None, data=None,
        concurrent_to=None, warc_type=None, content_type=None, remote_ip=None,
        profile=None, refers_to=None, refers_to_target_uri=None,
        refers_to_date=None):

        if warc_date is None:
            warc_date = warctools.warc.warc_datetime_str(datetime.now())

        record_id = warctools.WarcRecord.random_warc_uuid()

        headers = []
        if warc_type is not None:
            headers.append((warctools.WarcRecord.TYPE, warc_type))
        headers.append((warctools.WarcRecord.ID, record_id))
        if profile is not None:
            headers.append((warctools.WarcRecord.TYPE, profile))
        headers.append((warctools.WarcRecord.DATE, warc_date))
        headers.append((warctools.WarcRecord.URL, url))
        if refers_to is not None:
            headers.append((warctools.WarcRecord.REFERS_TO, refers_to))
        if refers_to_target_uri is not None:
            headers.append((warctools.WarcRecord.REFERS_TO_TARGET_URI, refers_to_target_uri))
        if refers_to_date is not None:
            headers.append((warctools.WarcRecord.REFERS_TO_DATE, refers_to_date))
        if remote_ip is not None:
            headers.append((warctools.WarcRecord.IP_ADDRESS, remote_ip))
        if concurrent_to is not None:
            headers.append((warctools.WarcRecord.CONCURRENT_TO, concurrent_to))
        if content_type is not None:
            headers.append((warctools.WarcRecord.CONTENT_TYPE, content_type))


        if recorder is not None:
            headers.append((warctools.WarcRecord.CONTENT_LENGTH, str(len(recorder))))
            headers.append((warctools.WarcRecord.BLOCK_DIGEST, 
                'sha1:{}'.format(self.digest_str(recorder.block_sha1))))
            if recorder.payload_sha1 is not None:
                headers.append((warctools.WarcRecord.PAYLOAD_DIGEST, 
                    'sha1:{}'.format(self.digest_str(recorder.payload_sha1))))

            recorder.tempfile.seek(0)
            record = warctools.WarcRecord(headers=headers, content_file=recorder.tempfile)

        else:
            headers.append((warctools.WarcRecord.CONTENT_LENGTH, str(len(data))))
            headers.append((warctools.WarcRecord.BLOCK_DIGEST, 
                'sha1:{}'.format(self.digest_str(hashlib.sha1(data)))))

            content_tuple = content_type, data
            record = warctools.WarcRecord(headers=headers, content=content_tuple)

        return record, record_id


class WarcWriterThread(threading.Thread):

    # port is only used for warc filename 
    def __init__(self, recordset_q, directory, rollover_size=1000000000, rollover_idle_time=None, gzip=False, prefix='WARCPROX', port=0):
        threading.Thread.__init__(self, name='WarcWriterThread')

        self.recordset_q = recordset_q

        self.rollover_size = rollover_size
        self.rollover_idle_time = rollover_idle_time

        self.gzip = gzip

        # warc path and filename stuff
        self.directory = directory
        self.prefix = prefix
        self.port = port

        self._f = None
        self._fpath = None
        self._serial = 0
        
        if not os.path.exists(directory):
            logging.info("warc destination directory {} doesn't exist, creating it".format(directory))
            os.mkdir(directory)

        self.stop = threading.Event()

        self.listeners = []


    def timestamp17(self):
        now = datetime.now()
        return '{}{}'.format(now.strftime('%Y%m%d%H%M%S'), now.microsecond//1000)

    def _close_writer(self):
        if self._fpath:
            final_name = self._fpath[:-5]
            logging.info('closing {0}'.format(final_name))
            self._f.close()
            os.rename(self._fpath, final_name)

            self._fpath = None
            self._f = None

    def _make_warcinfo_record(self, filename):
        warc_record_date = warctools.warc.warc_datetime_str(datetime.now())
        record_id = warctools.WarcRecord.random_warc_uuid()

        headers = []
        headers.append((warctools.WarcRecord.ID, record_id))
        headers.append((warctools.WarcRecord.TYPE, warctools.WarcRecord.WARCINFO))
        headers.append((warctools.WarcRecord.FILENAME, filename))
        headers.append((warctools.WarcRecord.DATE, warc_record_date))

        warcinfo_fields = []
        warcinfo_fields.append('software: warcprox.py https://github.com/internetarchive/warcprox')
        hostname = socket.gethostname()
        warcinfo_fields.append('hostname: {0}'.format(hostname))
        warcinfo_fields.append('ip: {0}'.format(socket.gethostbyname(hostname)))
        warcinfo_fields.append('format: WARC File Format 1.0')
        warcinfo_fields.append('robots: ignore')   # XXX implement robots support
        # warcinfo_fields.append('description: {0}'.format(self.description))   
        # warcinfo_fields.append('isPartOf: {0}'.format(self.is_part_of))   
        data = '\r\n'.join(warcinfo_fields) + '\r\n'

        record = warctools.WarcRecord(headers=headers, content=('application/warc-fields', data))

        return record


    # <!-- <property name="template" value="${prefix}-${timestamp17}-${serialno}-${heritrix.pid}~${heritrix.hostname}~${heritrix.port}" /> -->
    def _writer(self):
        if self._fpath and os.path.getsize(self._fpath) > self.rollover_size:
            self._close_writer()

        if self._f == None:
            filename = '{}-{}-{:05d}-{}-{}-{}.warc{}'.format(
                    self.prefix, self.timestamp17(), self._serial, os.getpid(),
                    socket.gethostname(), self.port, '.gz' if self.gzip else '')
            self._fpath = os.path.sep.join([self.directory, filename + '.open'])

            self._f = open(self._fpath, 'wb')

            warcinfo_record = self._make_warcinfo_record(filename)
            warcinfo_record.write_to(self._f, gzip=self.gzip)

            self._serial += 1

        return self._f


    def register_listener(self, listener):
        """listener should be a function that takes 3 arguments (record, warcfile, offset)"""
        self.listeners.append(listener)


    def run(self):
        logging.info('WarcWriterThread starting, directory={} gzip={} rollover_size={} rollover_idle_time={} prefix={} port={}'.format(
                os.path.abspath(self.directory), self.gzip, self.rollover_size, 
                self.rollover_idle_time, self.prefix, self.port))

        self._last_activity = time.time()

        while not self.stop.is_set():
            try:
                recordset = self.recordset_q.get(block=True, timeout=0.5)
                
                self._last_activity = time.time()
            
                writer = self._writer()

                for record in recordset:
                    offset = writer.tell()
                    record.write_to(writer, gzip=self.gzip)
                    logging.info('wrote warc record: warc_type={} content_length={} url={} warc={} offset={}'.format(
                            record.get_header(warctools.WarcRecord.TYPE),
                            record.get_header(warctools.WarcRecord.CONTENT_LENGTH),
                            record.get_header(warctools.WarcRecord.URL),
                            self._fpath, offset))

                    for listener in self.listeners:
                        listener(record, self._fpath, offset)

                self._f.flush()

            except Queue.Empty:
                if (self._fpath is not None 
                        and self.rollover_idle_time is not None 
                        and self.rollover_idle_time > 0
                        and time.time() - self._last_activity > self.rollover_idle_time):
                    logging.info('rolling over warc file after {} seconds idle'.format(time.time() - self._last_activity))
                    self._close_writer()

        logging.info('WarcWriterThread shutting down')
        self._close_writer();


if __name__ == '__main__':

    arg_parser = argparse.ArgumentParser(
            description='warcprox - WARC writing MITM HTTP/S proxy',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg_parser.add_argument('-p', '--port', dest='port', default='8080', 
            help='port to listen on')
    arg_parser.add_argument('-b', '--address', dest='address', 
            default='localhost', help='address to listen on')
    arg_parser.add_argument('-c', '--cacert', dest='cacert', 
            default='./{0}-warcprox-ca.pem'.format(socket.gethostname()), 
            help='CA certificate file; if file does not exist, it will be created')
    arg_parser.add_argument('--certs-dir', dest='certs_dir', 
            default='./{0}-warcprox-ca'.format(socket.gethostname()), 
            help='where to store and load generated certificates')
    arg_parser.add_argument('-d', '--dir', dest='directory', 
            default='./warcs', help='where to write warcs')
    arg_parser.add_argument('-z', '--gzip', dest='gzip', action='store_true', 
            help='write gzip-compressed warc records')
    arg_parser.add_argument('-n', '--prefix', dest='prefix', 
            default='WARCPROX', help='WARC filename prefix')
    arg_parser.add_argument('-s', '--size', dest='size', 
            default=1000*1000*1000, 
            help='WARC file rollover size threshold in bytes')
    arg_parser.add_argument('--rollover-idle-time', 
            dest='rollover_idle_time', default=None, 
            help="WARC file rollover idle time threshold in seconds (so that Friday's last open WARC doesn't sit there all weekend waiting for more data)")
    arg_parser.add_argument('--base32', dest='base32', action='store_true',
            default=False, help='write SHA1 digests in Base32 instead of hex')
    # arg_parser.add_argument('-j', '--dedup-db-file', dest='dedup_db_file',
    #         default='./dedup.db', help='persistent deduplication database file')
    arg_parser.add_argument('-v', '--verbose', dest='verbose', action='store_true')
    arg_parser.add_argument('-q', '--quiet', dest='quiet', action='store_true')
    # [--ispartof=warcinfo ispartof]
    # [--description=warcinfo description]
    # [--operator=warcinfo operator]
    # [--httpheader=warcinfo httpheader]
    args = arg_parser.parse_args()

    if args.verbose:
        loglevel = logging.DEBUG
    elif args.quiet:
        loglevel = logging.WARNING
    else:
        loglevel = logging.INFO

    logging.basicConfig(stream=sys.stdout, level=loglevel, 
            format='%(asctime)s %(process)d %(threadName)s %(levelname)s %(funcName)s(%(filename)s:%(lineno)d) %(message)s')

    dedup_db = DedupDb()

    recordset_q = WarcRecordsetQueue(base32=args.base32, dedup_db=dedup_db)

    proxy = WarcProxy(server_address=(args.address, int(args.port)),
            ca_file=args.cacert, certs_dir=args.certs_dir,
            recordset_q=recordset_q)

    warc_writer = WarcWriterThread(recordset_q=recordset_q,
            directory=args.directory, gzip=args.gzip, prefix=args.prefix,
            port=int(args.port), rollover_size=int(args.size), 
            rollover_idle_time=int(args.rollover_idle_time) if args.rollover_idle_time is not None else None)

    def close_content_file(record, warcfile, offset):
        if record.content_file:
            logging.info('closing record.content_file={}'.format(record.content_file))
            record.content_file.close()

    warc_writer.register_listener(close_content_file)
    warc_writer.register_listener(dedup_db.warc_record_written)

    proxy_thread = threading.Thread(target=proxy.serve_forever, name='ProxyThread')
    proxy_thread.start()
    warc_writer.start()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, stop.set)

    try:
        while not stop.is_set():
            time.sleep(0.5)
    except:
        pass
    finally:
        warc_writer.stop.set()
        proxy.shutdown()
        proxy.server_close()

