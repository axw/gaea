from protorpc import messages
from protorpc import remote

class HelloRequest(messages.Message):
    my_name = messages.StringField(1, required=True)

class HelloResponse(messages.Message):
    hello = messages.StringField(1, required=True)

class HelloService(remote.Service):   
    @remote.method(HelloRequest, HelloResponse)
    def hello(self, request):
        return HelloResponse(hello='Hello there, %s!' % request.my_name)

