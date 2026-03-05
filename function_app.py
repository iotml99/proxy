import azure.functions as func
from proxy import main

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="hello", methods=["GET"])
def hello(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("Hello, World! Proxy is running.", status_code=200)


@app.route(route="{*path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(req: func.HttpRequest) -> func.HttpResponse:
    return await main(req)
