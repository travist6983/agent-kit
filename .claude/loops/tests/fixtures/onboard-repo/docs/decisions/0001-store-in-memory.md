# 0001: keep the store in memory

The fixture has no database so tests stay fast and offline. A real store would sit behind
the same four methods (insert, get, update, all), so the service never changes when it lands.
