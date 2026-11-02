"""Explicit crop arguments avoid changing the running sorter's imaging globals."""


def crop_options(settings):
    cfg=settings.get('imaging',{})
    strength=cfg.get('clahe',0)
    if isinstance(strength,bool):strength=3.0 if strength else 0.0
    return {'donut':cfg.get('head_donut',True),'pocket_frac':cfg.get('pocket_frac',0.42),
            'crop_mode':cfg.get('crop_mode','normal'),'rim_scale':cfg.get('rim_scale',1.0),
            'clahe':strength,'enhance':cfg.get('enhance') or ('clahe' if strength else 'none'),
            'enhance_size':cfg.get('enhance_size',13)}
