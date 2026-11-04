"""Characterize notification display precedence without runtime or transport access."""

import builtins
from types import SimpleNamespace

import pytest

import notify_i18n_support as subject


@pytest.fixture
def trace(monkeypatch):
    events = []

    def locale():
        events.append(("locale",))
        return "en"

    def configured(kind, profile, locale):
        events.append(("configured", kind, profile, locale))
        return profile

    monkeypatch.setattr(subject, "get_notify_lang", locale)
    monkeypatch.setattr(subject, "display_name", configured)
    return events


def translator(events, result=None):
    def translate(key):
        events.append(("translate", key))
        return key if result is None else result

    return translate


def metadata_import(monkeypatch, events, *, resolver=None, error=None):
    original = builtins.__import__

    def load(name, *args, **kwargs):
        if name == "quant_platform_kit.common.notification_localization":
            events.append(("metadata_import",))
            if error is not None:
                raise error
            return SimpleNamespace(resolve_strategy_display_name=resolver)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", load)


def test_factory_does_not_resolve_until_called(trace):
    resolve = subject.build_strategy_display_name(translator(trace))
    assert trace == []
    assert resolve("custom", fallback_name=" Operator label ") == "Operator label"
    assert trace == [
        ("locale",), ("configured", "strategies", "custom", "en"),
        ("translate", "strategy_name_custom"),
    ]


@pytest.mark.parametrize("configured", ["Configured label", "", None, False, 0])
def test_configured_result_precedes_metadata_and_translation(monkeypatch, trace, configured):
    monkeypatch.setattr(subject, "display_name", lambda *args: configured)
    metadata_import(monkeypatch, trace, error=AssertionError("metadata must remain lazy"))
    result = subject.build_strategy_display_name(translator(trace))("custom", metadata=object())
    assert result is configured
    assert trace == [("locale",)]


def test_locale_default_is_evaluated_before_translator_attribute(monkeypatch, trace):
    class Translator:
        @property
        def notification_locale(self):
            trace.append(("locale_attribute",))
            return "zh"

        def __call__(self, key):
            trace.append(("translate", key))
            return key

    resolve = subject.build_strategy_display_name(Translator())
    assert resolve("custom") == "custom"
    assert trace == [
        ("locale",), ("locale_attribute",),
        ("configured", "strategies", "custom", "zh"),
        ("translate", "strategy_name_custom"),
    ]


def test_locale_error_propagates_even_with_translator_locale(monkeypatch, trace):
    error = LookupError("synthetic locale failure")

    def fail():
        raise error

    translate = translator(trace)
    translate.notification_locale = "zh"
    monkeypatch.setattr(subject, "get_notify_lang", fail)
    with pytest.raises(LookupError) as observed:
        subject.build_strategy_display_name(translate)("custom")
    assert observed.value is error
    assert trace == []


@pytest.mark.parametrize("metadata", [object(), {}, False, 0])
def test_metadata_result_and_arguments_are_preserved(monkeypatch, trace, metadata):
    expected = object()
    translate = translator(trace)

    def resolve(locale, provided, *, translator):
        trace.append(("metadata_resolve", locale, provided, translator))
        return expected

    metadata_import(monkeypatch, trace, resolver=resolve)
    result = subject.build_strategy_display_name(translate)("custom", metadata=metadata)
    assert result is expected
    assert trace == [
        ("locale",), ("configured", "strategies", "custom", "en"),
        ("metadata_import",), ("locale",),
        ("metadata_resolve", "en", metadata, translate),
    ]


def test_import_error_falls_through_to_legacy(monkeypatch, trace):
    metadata_import(monkeypatch, trace, error=ImportError("synthetic unavailable resolver"))
    result = subject.build_strategy_display_name(translator(trace))(
        " custom ", metadata=object(), fallback_name="  Operator label  ",
    )
    assert result == "Operator label"
    assert trace == [
        ("locale",), ("configured", "strategies", " custom ", "en"),
        ("metadata_import",), ("translate", "strategy_name_custom"),
    ]


@pytest.mark.parametrize("error", [ImportError("resolver failed"), ValueError("resolver failed")])
def test_resolver_execution_error_does_not_fall_through(monkeypatch, trace, error):
    def fail(*args, **kwargs):
        raise error

    metadata_import(monkeypatch, trace, resolver=fail)
    with pytest.raises(type(error)) as observed:
        subject.build_strategy_display_name(translator(trace))("custom", metadata=object())
    assert observed.value is error
    assert trace == [
        ("locale",), ("configured", "strategies", "custom", "en"),
        ("metadata_import",), ("locale",),
    ]


@pytest.mark.parametrize("translated", ["Translated label", "", None, False, 0, object()])
def test_legacy_translation_result_is_returned_without_coercion(monkeypatch, trace, translated):
    metadata_import(monkeypatch, trace, error=AssertionError("no metadata import expected"))

    class UnusableFallback:
        def __bool__(self):
            raise AssertionError("fallback must remain lazy")

    def translate(key):
        trace.append(("translate", key))
        return translated

    result = subject.build_strategy_display_name(translate)(" custom ", fallback_name=UnusableFallback())
    assert result is translated
    assert trace[-1] == ("translate", "strategy_name_custom")


@pytest.mark.parametrize("profile, fallback, expected, key", [
    (" custom ", " Operator label ", "Operator label", "strategy_name_custom"),
    (" 示例策略 ", "", "示例策略", "strategy_name_示例策略"),
    (" custom ", None, "custom", "strategy_name_custom"),
    (" custom ", False, "custom", "strategy_name_custom"),
    (" custom ", 0, "custom", "strategy_name_custom"),
    (" custom ", 42, "42", "strategy_name_custom"),
    (None, " ", "", "strategy_name_"),
    (False, " ", "", "strategy_name_"),
    (0, " ", "", "strategy_name_"),
])
def test_legacy_fallback_normalization(trace, profile, fallback, expected, key):
    result = subject.build_strategy_display_name(translator(trace))(profile, fallback_name=fallback)
    assert result == expected
    assert trace[-1] == ("translate", key)


def test_legacy_coercion_order_and_repeated_profile_read(trace):
    class TracedValue:
        def __init__(self, name, text):
            self.name, self.text = name, text

        def __bool__(self):
            trace.append(("bool", self.name))
            return True

        def __str__(self):
            trace.append(("str", self.name))
            return self.text

    profile, fallback = TracedValue("profile", " custom "), TracedValue("fallback", " ")
    result = subject.build_strategy_display_name(translator(trace))(profile, fallback_name=fallback)
    assert result == "custom"
    assert trace == [
        ("locale",), ("configured", "strategies", profile, "en"),
        ("bool", "profile"), ("str", "profile"), ("translate", "strategy_name_custom"),
        ("bool", "fallback"), ("str", "fallback"), ("bool", "profile"), ("str", "profile"),
    ]


def test_legacy_translation_exception_preserves_identity(trace):
    error = RuntimeError("synthetic translation failure")

    def translate(key):
        trace.append(("translate", key))
        raise error

    with pytest.raises(RuntimeError) as observed:
        subject.build_strategy_display_name(translate)(" custom ", fallback_name="Unused")
    assert observed.value is error
    assert trace[-1] == ("translate", "strategy_name_custom")


def test_legacy_fallback_coercion_exception_preserves_identity(trace):
    error = ValueError("synthetic fallback failure")

    class InvalidFallback:
        def __str__(self):
            trace.append(("fallback_str",))
            raise error

    with pytest.raises(ValueError) as observed:
        subject.build_strategy_display_name(translator(trace))("custom", fallback_name=InvalidFallback())
    assert observed.value is error
    assert trace[-2:] == [("translate", "strategy_name_custom"), ("fallback_str",)]
