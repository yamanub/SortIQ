"""Transport between the application (Pi side) and the motion controller.

The board adapter (Cs72Transport in sorter/cs72.py) implements this
interface over USB serial; SimTransport runs the in-process simulated controller
(sorter/esp32_sim.py) for the simulator mode. The app only ever calls
send() / readline().
"""
import queue


class Transport:
    def send(self, line: str):
        raise NotImplementedError

    def readline(self, timeout: float = None) -> str | None:
        """Next line from the controller, or None on timeout."""
        raise NotImplementedError

    def close(self):
        pass


class SimTransport(Transport):
    """Wraps Esp32Sim; lines flow through queues instead of a UART."""

    def __init__(self, sim):
        self.sim = sim
        sim.attach(self)
        self.to_app = queue.Queue()

    def send(self, line):
        self.sim.handle_line(line.strip())

    def readline(self, timeout=None):
        try:
            return self.to_app.get(timeout=timeout)
        except queue.Empty:
            return None

    # called by the sim ("firmware -> Pi" direction)
    def emit(self, line):
        self.to_app.put(line)

    def close(self):
        self.sim.stop()
