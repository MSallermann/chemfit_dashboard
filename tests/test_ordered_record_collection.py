from chemfit_dashboard.ordered_record_collection import OrderedRecordCollection
from chemfit_dashboard.source import JsonFileSource


def test_invert_then_reset_restores_natural_order_and_metadata(tmp_path):
    for name in ("step_10.json", "step_2.json", "step_1.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    collection = OrderedRecordCollection()
    collection.replace(JsonFileSource().discover([tmp_path]))

    collection.invert_order()
    assert [record.name for record in collection] == [
        "step_10.json",
        "step_2.json",
        "step_1.json",
    ]
    assert collection.order_label == "natural"
    assert collection.ascending is False

    collection.reset_order()

    assert [record.name for record in collection] == [
        "step_1.json",
        "step_2.json",
        "step_10.json",
    ]
    assert collection.order == collection.natural_order
    assert collection.order_label == "natural"
    assert collection.ascending is True
