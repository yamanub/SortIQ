"""TFLite interpreter loading — the one piece every model consumer shares.

Uses tflite_runtime on the Pi (Python <= 3.11), ai-edge-litert on Pi OS
trixie / Python 3.12+, and full TensorFlow's bundled interpreter on the
PC. Same file, same weights, all places. (The softmax classifier classes
that used to live here have been retired — the sorting
brain is sorter/embed_classifier.py.)
"""


def _load_interpreter(path):
    try:
        from tflite_runtime.interpreter import Interpreter   # Pi, Python <= 3.11
    except ImportError:
        try:
            # Google's tflite-runtime successor (Pi OS trixie / Python 3.12+)
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:
            import tensorflow as tf                           # on the PC
            Interpreter = tf.lite.Interpreter
    import os
    # default is ONE thread — on the quad-core Pis that left 1.9x on the
    # table (Pi 4 bench: 352 -> 187 ms/invoke on the shadow embed). Leave
    # one core for the camera pump + web server.
    threads = max(2, (os.cpu_count() or 2) - 1)
    interp = Interpreter(model_path=str(path), num_threads=threads)
    interp.allocate_tensors()
    return interp
