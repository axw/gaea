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

from __future__ import with_statement

from google.appengine.api import channel, memcache, users, xmpp
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

from django.utils import simplejson
import protorpc
import protorpc.descriptor
import protorpc.definition
import protorpc.transport

import base64
import contextlib
import logging
import marshal as _marshal
import os
import sys
import types

###############################################################################
# Common classes/data.
###############################################################################


class StdoutRedirector(object):
    def __init__(self, client):
        self.__client = client
    def write(self, s):
        channel.send_message(self.__client, simplejson.dumps(s))
    def flush(self):
        pass
    def __getattr__(self, s):
        return getattr(sys.__stdout__, s)


@contextlib.contextmanager
def stdout_to_channel(client):
    orig_stdout = sys.stdout
    sys.stdout = StdoutRedirector(client)
    yield
    sys.stdout = orig_stdout


class XMPPState(object):
    """
    Object for storing the state of our XMPP roster.
    """

    def __init__(self):
        self.__client = memcache.Client()
        while self.available is None:
            self.__client.add("available", {})


    def on_available(self, jid):
        available = self.available
        while available is not None and jid not in available:
            available[jid] = None
            if not self.__client.cas("available", available):
                available = self.available


    def on_unavailable(self, jid):
        available = self.available
        while available is not None and jid in available:
            del available[jid]
            if not self.__client.cas("available", available):
                available = self.available


    def set_service(self, jid, service):
        available = self.available
        while available is not None and available[jid] != service:
            available[jid] = service
            if not self.__client.cas("available", available):
                available = self.available


    def get_service(self, jid):
        encoded_descriptor, service_name = self.available[jid]
        descriptor = protorpc.protobuf.decode_message(
            protorpc.descriptor.FileDescriptor, encoded_descriptor)

        # Decode and define the service stub.
        #
        # It appears there's a defect in protorpc (or perhaps an enhancement in
        # GAE's Python?), where it attempts to create a module with a unicode
        # name. We can get around this by creating the module ourselves, with
        # a sanitized name.
        #
        # We also need to set the module inside the module, or define_service
        # will fail to find the request/response message types.
        module = types.ModuleType(str(descriptor.package))
        setattr(module, descriptor.package, module)
        protorpc.definition.define_file(descriptor, module)
        protorpc.definition.define_file(descriptor, module)
        return getattr(module, service_name)


    @property
    def available(self):
        return self.__client.gets("available")


# Instantiate XMPPState.
xmpp_state = XMPPState()

# Define message IDs.
MSG_GET_DESCRIPTOR = 0
MSG_DESCRIPTOR     = 1
MSG_REQUEST        = 2
MSG_RESPONSE       = 3

###############################################################################
# Main page handler.
###############################################################################


class MainPage(webapp.RequestHandler):
    def get(self):
        user = users.get_current_user()
        template_values = {
            'channel_token': channel.create_channel(user.user_id()),
            'email': user.email(),
        }
        path = os.path.join(os.path.dirname(__file__), 'index.html')
        self.response.out.write(template.render(path, template_values))


class AvailablePage(webapp.RequestHandler):
    def get(self):
        self.response.headers.add_header("Content-Type", "text/json")
        self.response.out.write(simplejson.dumps(
            {"agents": xmpp_state.available.keys()}))


class MethodWrapper(object):
    def __init__(self, service, remote, jid):
        self.__service = service
        self.__remote = remote
        self.__jid = jid


    def __call__(self, request):
        user = users.get_current_user()
        transport = XMPPTransport(self.__jid, user.user_id())
        method_name = self.__remote.method.__name__
        getattr(self.__service.AsyncStub(transport), method_name)(request)


    def __getattr__(self, name):
        return getattr(self.__remote, name)


    @property
    def remote(self):
        return self.__remote


class ExecutePage(webapp.RequestHandler):
    def post(self):
        agent = self.request.get("agent")
        source = self.request.get("code").strip()

        # Prepare the protorpc service method stubs.
        service = xmpp_state.get_service(agent)
        methods = service.all_remote_methods()
        for name, method in service.all_remote_methods().items():
            wrapper = MethodWrapper(service, method.remote, agent)
            setattr(service, name, wrapper)

        # Execute the code.
        with stdout_to_channel(users.get_current_user().user_id()):
            globals_ = {"agent": service}
            codeobj = compile(source, "<web>", "exec")
            eval(codeobj, globals_)


###############################################################################
# XMPP handlers.
###############################################################################


def marshal(*args):
    return base64.b64encode(_marshal.dumps(args))


def unmarshal(encoded):
    return _marshal.loads(base64.b64decode(encoded))


class XMPPTransport(protorpc.transport.Transport):
    def __init__(self, jid, context):
        super(XMPPTransport, self).__init__(protocol=protorpc.protobuf)
        self.__jid = jid
        self.__context = context


    def _start_rpc(self, remote_info, request):
        encoded_request = self.protocol.encode_message(request)
        msg = marshal(MSG_REQUEST,
                      remote_info.method.func_name,
                      encoded_request,
                      (remote_info.method.func_name, self.__context))
        assert xmpp.send_message(self.__jid, msg) == xmpp.NO_ERROR
        return protorpc.transport.Rpc(request)



class XMPPHandler(webapp.RequestHandler):
    def __init__(self, *args, **kwargs):
        webapp.RequestHandler.__init__(self, *args, **kwargs)
        self.__handlers = {
            MSG_DESCRIPTOR: self.__handle_descriptor,
            MSG_RESPONSE: self.__handle_response
        }


    def post(self):
        from_jid = self.request.get("from")
        message = unmarshal(self.request.get("body"))
        assert type(message) is tuple
        self.__handlers[message[0]](from_jid, message)


    def __handle_descriptor(self, from_jid, message):
        encoded_descriptor = message[1]
        service_name = message[2]
        logging.info("Encoded descriptor: %r", encoded_descriptor)
        xmpp_state.set_service(from_jid, (encoded_descriptor, service_name))


    def __handle_response(self, from_jid, message):
        logging.info("Received response: %r", message)
        result, context = message[1:]
        funcname = context[0]

        service = xmpp_state.get_service(from_jid)
        remote = getattr(service, funcname).remote
        response = protorpc.protobuf.decode_message(
            remote.response_type, result)
        logging.info("Decoded response: %r", response)

        # Send the result back down the channel.
        user_id = context[1]
        channel.send_message(user_id, simplejson.dumps(str(response)))


class XMPPAvailableHandler(webapp.RequestHandler):
    def post(self):
        # Record the peer's availability in the XMPPState object.
        from_jid = self.request.get("from")
        slash = from_jid.find("/")
        if slash == -1 or not from_jid[slash+1:].startswith("gaea"):
            logging.info("Ignoring JID: %r", from_jid)
            return
        else:
            logging.info("Available: %r", from_jid)
            xmpp_state.on_available(from_jid)

            # Request the peer's service descriptor.
            msg = marshal(MSG_GET_DESCRIPTOR)
            assert xmpp.send_message(from_jid, msg) == xmpp.NO_ERROR


class XMPPUnavailableHandler(webapp.RequestHandler):
    def post(self):
        from_jid = self.request.get("from")
        xmpp_state.on_unavailable(from_jid)
        logging.info("Unavailable: %r", from_jid)


###############################################################################
# WSGI boilerplate.
###############################################################################


application = webapp.WSGIApplication([
    ('/', MainPage),
    ('/available', AvailablePage),
    ('/execute', ExecutePage),
    ('/_ah/xmpp/message/chat/', XMPPHandler),
    ('/_ah/xmpp/presence/available/', XMPPAvailableHandler),
    ('/_ah/xmpp/presence/unavailable/', XMPPUnavailableHandler),
], debug=True)

def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()

