"""Per-client PyMax 2.4.1 photo upload compatibility.

The signed URL is opaque. A missing photoIds query is supported only when
HTTP returns exactly one validated photo/token. Other SDK uploads delegate
unchanged, retaining their notification waiters and handlers.
"""
from contextvars import ContextVar
import hashlib
import logging
import secrets
import json
from urllib.parse import urlsplit, parse_qs
import aiohttp
from .errors import MaxTransportError, MaxUnsupportedError, MaxAuthError

logger = logging.getLogger(__name__)
phase=ContextVar("max_upload_phase",default="idle")

class DeliveryConfirmationUncertain(MaxUnsupportedError):
    """Do not automatically repeat a send whose confirmation was lost."""


class SDKMediaLogFilter(logging.Filter):
    """Redact SDK records only in the current SHMAX media task context."""
    def filter(self,record):
        if phase.get()!='idle':
            category=record.exc_info[0].__name__ if record.exc_info else 'none'
            record.msg='MAX SDK media event exception='+category
            record.args=();record.exc_info=None;record.exc_text=None;record.stack_info=None
        return True


class PhotoUploadProtocolError(MaxUnsupportedError):
    """Permanent incompatible response: retain queue item for manual review."""


def schema(value):
    # Emit structural names, never attacker-selected arbitrary keys/values.
    allowed={'url','info','photos','token','error','payload','photoIds','photoId'}
    return ','.join(sorted(key for key in value if key in allowed)) if isinstance(value,dict) else type(value).__name__


class PhotoUploadCompatibility:
    def __init__(self, original):
        self.original=original
        self.app=original.app

    def __getattr__(self,name):
        return getattr(self.original,name)

    async def upload_file(self,file):
        phase.set("upload")
        result=await self.original.upload_file(file)
        phase.set("uploaded")
        return result

    async def upload_photo(self,photo,profile=False):
        if profile:
            return await self.original.upload_photo(photo,profile=True)
        phase.set("upload")
        from pymax.api.uploads.payloads import UploadPayload,AttachPhotoPayload
        from pymax.api.uploads.models import PhotoUploadResponse
        from pymax.protocol import Opcode
        correlation=hashlib.sha256(secrets.token_bytes(16)).hexdigest()[:12]
        operation='max.photo_init'
        try:
            data=await self.app.invoke(Opcode.PHOTO_UPLOAD,payload=UploadPayload(profile=profile).to_payload())
            payload=data.payload
            url=payload.get('url') if isinstance(payload,dict) else None
            if not isinstance(url,str) or not url:
                logger.warning('upload type=photo operation=%s schema=%s reason=missing_url correlation=%s',operation,schema(payload),correlation)
                raise PhotoUploadProtocolError('photo_upload_initialization_invalid')
            parsed=urlsplit(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                raise PhotoUploadProtocolError('photo_upload_url_invalid')
            ids=parse_qs(parsed.query).get('photoIds',[])
            logger.info('upload type=photo operation=%s schema=%s photo_ids_present=%s correlation=%s',operation,schema(payload),bool(ids),correlation)
            valid=photo.validate_photo()
            if not valid or await photo.size()>20*1024*1024:
                raise PhotoUploadProtocolError('photo_upload_input_invalid')
            form=aiohttp.FormData()
            form.add_field('file',await photo.read(),filename='image.'+valid[0],content_type=valid[1])
            operation='max.photo_http'
            timeout=aiohttp.ClientTimeout(total=self.app.config.upload_timeout,sock_read=30)
            async with aiohttp.ClientSession(proxy=self.app.config.proxy,timeout=timeout) as session:
                async with session.post(url,data=form,allow_redirects=False) as response:
                    logger.info('upload type=photo operation=%s http_status=%s correlation=%s',operation,response.status,correlation)
                    if response.status!=200:
                        if response.status in {401,403}:raise MaxAuthError("photo_upload_authorization_rejected")
                        if response.status in {400,404,413,415}:
                            raise PhotoUploadProtocolError('photo_upload_http_rejected')
                        raise MaxTransportError('photo_upload_http_transient')
                    # Bound JSON before parsing, including hostile chunked responses.
                    blocks=[];size=0
                    async for block in response.content.iter_chunked(65536):
                        size+=len(block)
                        if size>1024*1024:raise PhotoUploadProtocolError("photo_upload_response_too_large")
                        blocks.append(block)
                    raw=b"".join(blocks)
                    result=json.loads(raw)
            operation='max.photo_descriptor'
            logger.info('upload type=photo operation=%s schema=%s correlation=%s',operation,schema(result),correlation)
            model=PhotoUploadResponse.model_validate(result)
            if ids:
                if len(ids)!=1 or ',' in ids[0] or ids[0] not in model.photos:
                    raise PhotoUploadProtocolError('photo_upload_id_mismatch')
                token=model.photos[ids[0]].token
            else:
                if len(model.photos)!=1:raise PhotoUploadProtocolError('photo_upload_response_ambiguous')
                token=next(iter(model.photos.values())).token
            if not token:raise PhotoUploadProtocolError('photo_upload_token_missing')
            phase.set("uploaded")
            return AttachPhotoPayload(photo_token=token)
        except (PhotoUploadProtocolError,MaxTransportError,MaxAuthError):raise
        except (ValueError,TypeError,KeyError,AttributeError):
            logger.warning('upload type=photo operation=%s reason=invalid_schema correlation=%s',operation,correlation)
            raise PhotoUploadProtocolError('photo_upload_response_invalid') from None
        except Exception as exc:
            logger.warning('upload type=photo operation=%s exception=%s reason=request_failed correlation=%s',operation,type(exc).__name__,correlation)
            raise MaxTransportError('photo_upload_request_failed') from None


def install(client):
    app=getattr(client,'_app',None)
    if app is not None and not isinstance(app.api.uploads,PhotoUploadCompatibility):
        # No SDK function/class is globally patched. Logging filters are scoped
        # by ContextVar so other clients and non-media operations are unaffected.
        for name,sdk_logger in list(logging.Logger.manager.loggerDict.items()):
            if name.startswith("pymax.") and isinstance(sdk_logger,logging.Logger) and not any(isinstance(f,SDKMediaLogFilter) for f in sdk_logger.filters):
                sdk_logger.addFilter(SDKMediaLogFilter())
        app.api.uploads=PhotoUploadCompatibility(app.api.uploads)
