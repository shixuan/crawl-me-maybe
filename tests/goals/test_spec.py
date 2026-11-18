from crawlme.goals import spec_fields, spec_time_field, spec_version


def test_spec_contract():
    spec = {"fields": {"date": "event date", "place": "venue"}, "time_field": {"name": "date", "kind": "on"}}
    assert spec_fields(spec) == spec["fields"]
    assert spec_time_field(spec) == ("date", "on")
    reordered = {"fields": {"place": "venue", "date": "event date"}, "time_field": spec["time_field"]}
    assert spec_version(spec) == spec_version(reordered)
    assert spec_version(spec, "event occurrence") != spec_version(spec)
    assert spec_version(None) == ""
    assert spec_time_field({"fields": {}, "time_field": spec["time_field"]}) is None
