"""Isolated SortIQ embedding inference; never installs or replaces sorting weights."""
import hashlib
import json
from pathlib import Path
import sys
import threading
from .image_settings import crop_options

CACHE={}
LOCK=threading.RLock()


def paths(root,store,profile):
    community=Path(store)/'community'/profile['cartridge']
    installed=Path(root)/'calibers'/profile['cartridge']/profile['model']/'models'
    for directory,source in ((community,'community'),(installed,'installed SortIQ')):
        model=directory/'shadow_embed.tflite';gallery=directory/'shadow_gallery.npz'
        if model.is_file() and gallery.is_file():return model,gallery,source
    return None


def status(root,store,profile):
    found=paths(root,store,profile)
    return {'available':found is not None,'source':found[2] if found else None,
            'community_available':bool(found and found[2]=='community'),
            'community_note':'No separate community model is installed. The installed SortIQ model provides the local opinion.' if not found or found[2]!='community' else 'Separate community model; sorting weights are unchanged.'}


def predict(root,store,path,profile,settings):
    found=paths(root,store,profile)
    if not found:return {'available':False,'error':'No local recognition pair is installed.'}
    model,gallery,source=found
    sys.path.insert(0,str(root))
    import cv2
    from sorter import imaging
    from sorter.embed_classifier import EmbedClassifier
    with LOCK:
        key=(str(model),model.stat().st_mtime_ns,str(gallery),gallery.stat().st_mtime_ns)
        classifier=CACHE.get(key)
        if classifier is None:
            import numpy as np
            with np.load(gallery,allow_pickle=False) as bank:
                meta=json.loads(str(bank['meta']))
            if meta.get('model_digest')!=hashlib.md5(model.read_bytes()).hexdigest():
                return {'available':False,'error':'Local model/gallery digest mismatch.'}
            classifier=EmbedClassifier(model,gallery)
            CACHE.clear();CACHE[key]=classifier
        image=cv2.imread(str(path))
        if image is None:return {'available':False,'error':'Unreadable image.'}
        result=classifier.predict(imaging.crop_head(image, **crop_options(settings)))
        return {'available':True,'source':source,'stamp':result.get('stamp'),
                'similarity':float(result.get('sim',0)),'margin':float(result.get('margin',0)),
                'accepted':bool(result.get('accept')),'model_digest':classifier.model_md5,
                'known_class':result.get('stamp') in settings.get('stamp_labels',[])}
