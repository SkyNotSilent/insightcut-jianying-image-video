"""Bound multipart bodies as chunks arrive, before parser spool files can grow."""
from fastapi import HTTPException
from starlette.responses import JSONResponse


class UploadBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') not in {'POST', 'PUT', 'PATCH'}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        if not headers.get(b'content-type', b'').startswith(b'multipart/form-data'):
            return await self.app(scope, receive, send)
        limit = (105 if scope['path'].endswith('/create-from-images') else 21) * 1024 * 1024
        try:
            oversized = int(headers.get(b'content-length', b'0')) > limit
        except ValueError:
            oversized = True
        if oversized:
            return await JSONResponse({'detail': '上传内容过大，请减少文件大小或数量'}, status_code=413)(scope, receive, send)
        count = 0
        async def bounded_receive():
            nonlocal count
            message = await receive()
            count += len(message.get('body', b''))
            if count > limit:
                raise HTTPException(status_code=413, detail='上传内容过大，请减少文件大小或数量')
            return message
        return await self.app(scope, bounded_receive, send)
