MAX_WIDGETS = 42


def add_widget(batch: list) -> None:
    if len(batch) >= MAX_WIDGETS:
        raise ValueError("E_LIMIT")
    batch.append(object())
