# Copyright (c) 2011 Andrew Wilkins <axwalk@gmail.com>
# 
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
# 
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import base64
import marshal as _marshal
import struct
import sys
import time

import protorpc
import protorpc.descriptor
import protorpc.definition
import protorpc.transport
import xmpp


# Define message IDs.
MSG_GET_DESCRIPTOR = 0
MSG_DESCRIPTOR     = 1
MSG_REQUEST        = 2
MSG_RESPONSE       = 3


def marshal(*args):
    return base64.b64encode(_marshal.dumps(args))


def unmarshal(encoded):
    return _marshal.loads(base64.b64decode(encoded))


class XMPPTransport(protorpc.transport.Transport):
    def __init__(self, local_jid, password, timeout=5.0):
        super(XMPPTransport, self).__init__(protocol=protorpc.protobuf)
        if type(local_jid) is str:
            local_jid = xmpp.protocol.JID(local_jid)
        self.__local_jid = local_jid
        self.__client = xmpp.Client(local_jid.getDomain(), debug=[])
        self.__response = None
        self.__timeout = timeout

        self.__handlers = {
            MSG_GET_DESCRIPTOR: self.__handle_get_descriptor,
            MSG_REQUEST: self.__handle_request
        }

        # Connect, authenticate and register the message handler.
        if not self.__client.connect(("talk.google.com", 5222), use_srv=False):
            raise Exception, "Failed to connect"
        if not self.__client.auth(local_jid.getNode(), password,
                                  local_jid.getResource()):
            raise Exception, "Failed to authenticate"
        self.__client.RegisterHandler("message", self.__receive_message)


    def register(self, with_jid):
        if type(with_jid) is str:
            with_jid = xmpp.protocol.JID(with_jid)
        self.__client.getRoster().Subscribe(with_jid)


    def start(self, service):
        self.__service = service
        self.__factory = service.new_factory()
        self.__client.sendPresence()


    def serve_forever(self, timeout=None):
        timeout_sec = timeout
        if timeout is not None:
            timeout = time.time() + timeout
        else:
            timeout_sec = 0.05
        while timeout is None or time.time() < timeout:
            self.__client.Process(timeout_sec)


    def __receive_message(self, conn, xmpp_msg):
        message = unmarshal(xmpp_msg.getBody())
        assert type(message) is tuple
        self.__handlers[message[0]](conn, xmpp_msg, message)


    def __handle_get_descriptor(self, conn, xmpp_msg, request):
        module = sys.modules[self.__service.__module__]
        descriptor = protorpc.descriptor.describe_file(module)
        encoded_descriptor = protorpc.protobuf.encode_message(descriptor)
        message = marshal(
            MSG_DESCRIPTOR, encoded_descriptor, self.__service.__name__)
        reply = xmpp_msg.buildReply(message)
        reply.setType("chat")
        self.__client.send(reply)


    def __handle_request(self, conn, xmpp_msg, request):
        funcname, encoded_request, context = request[1:]

        service = self.__factory()
        method = getattr(service, funcname)
        remote = getattr(method, "remote")
        request = protorpc.protobuf.decode_message(
            remote.request_type, encoded_request)
        response = method(request) # Execute the method.
        encoded_response = protorpc.protobuf.encode_message(response)
        message = marshal(MSG_RESPONSE, encoded_response, context)

        reply = xmpp_msg.buildReply(message)
        reply.setType("chat")
        self.__client.send(reply)


def run_client(email, application, password, service):
    # Create an XMPP transport object.
    transport = XMPPTransport(email + "/gaea", password)

    # Register with the application on Google App Engine.
    transport.register(application + "@appspot.com")
    transport.start(service)
    transport.serve_forever()


if __name__ == "__main__":
    import optparse
    import getpass
    import helloworld

    parser = optparse.OptionParser()
    parser.add_option("-e", "--email", dest="email")
    parser.add_option("-p", "--password", dest="password")
    parser.add_option("-a", "--application", dest="application",
                      help="Google App Engine application name")
    options, args = parser.parse_args()

    if not options.email:
        parser.error("Email address is required")
    if not options.application:
        parser.error("Application name is required")

    password = getattr(parser, "password", None)
    if not password:
        password = getpass.getpass("Password (%s): " % options.email)

    # Serve the "HelloWorld" service.
    run_client(
        options.email,
        options.application,
        password,
        helloworld.HelloService)

