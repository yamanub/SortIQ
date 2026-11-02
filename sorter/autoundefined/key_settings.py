"""Write-only OpenAI key settings; values never enter responses or application logs."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid


def configured(path):
    try:
        return Path(path).is_file() and Path(path).stat().st_size > 0
    except OSError:
        return False


def save(path, value, model):
    if not isinstance(value, str) or not value.strip().startswith('sk-'):
        raise ValueError('Enter the complete secret API key beginning with sk-. Tracking IDs are not API keys.')
    value=value.strip()
    if '...' in value or '…' in value or any(c.isspace() for c in value) or len(value)>1024:
        raise ValueError('Enter a complete, unmasked API key without spaces.')
    request=urllib.request.Request('https://api.openai.com/v1/models',headers={'Authorization':'Bearer '+value})
    try:
        with urllib.request.urlopen(request,timeout=20) as response:
            result=json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError('OpenAI rejected this key (HTTP %s). The existing key was kept.' % error.code) from None
    except (OSError,ValueError):
        raise ValueError('Could not validate the key with OpenAI. The existing key was kept.') from None
    if model not in {item.get('id') for item in result.get('data',[])}:
        raise ValueError('This key does not list '+model+'. Check project/model permissions. The existing key was kept.')
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    def atomic(destination,content):
        temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            if temporary.exists():temporary.unlink()
    # Rotation backup stays in the same protected secret directory, outside project archives.
    if configured(target):atomic(target.with_name('openai-key.previous'),target.read_text())
    atomic(target,value)
    return {'configured':True,'validated':True}
