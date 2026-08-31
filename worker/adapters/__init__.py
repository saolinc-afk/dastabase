from worker.adapters.fake import FakeAdapter


def __getattr__(name: str):
    if name == "AjpesEprsAdapter":
        from worker.adapters.ajpes_eprs import AjpesEprsAdapter

        return AjpesEprsAdapter

    raise AttributeError(name)


__all__ = ["AjpesEprsAdapter", "FakeAdapter"]
