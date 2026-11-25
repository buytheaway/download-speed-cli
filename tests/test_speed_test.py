import argparse
from collections.abc import Iterator
from unittest.mock import MagicMock, Mock, call

import pytest
import requests

import speed_test


URL = "https://example.com/file.bin"


@pytest.fixture(autouse=True)
def disable_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately if a test accidentally tries to use the network."""
    monkeypatch.delattr(requests.sessions.Session, "request")


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mocked_session = MagicMock(spec=requests.Session)
    mocked_session.__enter__.return_value = mocked_session
    monkeypatch.setattr(
        speed_test.requests, "Session", Mock(return_value=mocked_session)
    )
    return mocked_session


def make_response(
    chunks: Iterator[bytes], status_code: int = 200
) -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.__enter__.return_value = response
    response.status_code = status_code
    response.headers = {"Content-Length": "999999999"}
    response.iter_content.return_value = chunks
    return response


@pytest.mark.parametrize(
    ("byte_count", "elapsed_seconds", "expected"),
    [(1_000_000, 2.0, 4.0), (125_000, 0.5, 2.0), (0, 1.0, 0.0)],
)
def test_calculate_speed(
    byte_count: int, elapsed_seconds: float, expected: float
) -> None:
    assert speed_test.calculate_speed(byte_count, elapsed_seconds) == expected


def test_summary_uses_total_bytes_and_time(
    capsys: pytest.CaptureFixture,
) -> None:
    speed_test.print_summary([(1_000_000, 1.0), (9_000_000, 3.0)])

    output = capsys.readouterr().out
    assert "Среднее время запроса: 2.000 с" in output
    assert "Общий объём: 10000000 байт" in output
    assert "Суммарное время запросов: 4.000 с" in output
    assert "Средняя скорость: 20.000 Mbps" in output


def test_ten_requests_are_fully_read_and_closed_in_order(
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    events: list[str] = []
    clock_values = iter(float(value) for value in range(20))
    responses: list[MagicMock] = []

    def clock() -> float:
        events.append("time")
        return next(clock_values)

    def chunks(number: int) -> Iterator[bytes]:
        events.append(f"read {number}")
        yield b"abc"
        yield b""
        yield b"de"
        events.append(f"complete {number}")

    def get_response(*args: object, **kwargs: object) -> MagicMock:
        number = session.get.call_count
        events.append(f"get {number}")
        response = make_response(chunks(number))
        response.__exit__.side_effect = lambda *args: events.append(
            f"close {number}"
        )
        responses.append(response)
        return response

    monkeypatch.setattr(speed_test.time, "perf_counter", clock)
    session.get.side_effect = get_response

    assert speed_test.main([URL]) == 0

    expected_events = []
    for number in range(1, 11):
        expected_events.extend(
            [
                "time",
                f"get {number}",
                f"read {number}",
                f"complete {number}",
                f"close {number}",
                "time",
            ]
        )
    assert events == expected_events
    assert session.get.call_args_list == [
        call(
            URL,
            stream=True,
            headers={"Accept-Encoding": "identity"},
            timeout=(5.0, 30.0),
            allow_redirects=False,
        )
    ] * 10
    for response in responses:
        response.raise_for_status.assert_called_once_with()
        response.iter_content.assert_called_once_with(chunk_size=64 * 1024)
        response.__exit__.assert_called_once()
    session.__exit__.assert_called_once()

    output = capsys.readouterr()
    lines = output.out.splitlines()
    assert sum(line.startswith("Запрос ") for line in lines) == 10
    assert "Запрос 01/10: 1.000 с | 5 байт | 0.000 Mbps" in lines
    assert "Запрос 10/10: 1.000 с | 5 байт | 0.000 Mbps" in lines
    assert "Итоги: 10 успешных запросов" in output.out
    assert "Общий объём: 50 байт" in output.out
    assert "Суммарное время запросов: 10.000 с" in output.out
    assert output.err == ""


def test_empty_body_is_valid(
    session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = make_response(iter([]), status_code=204)
    session.get.return_value = response
    monkeypatch.setattr(
        speed_test.time, "perf_counter", Mock(side_effect=[10.0, 10.5])
    )

    assert speed_test.download_once(session, URL) == (0, 0.5)
    response.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectTimeout("connect timeout"),
        requests.ReadTimeout("read timeout"),
        requests.ConnectionError("connection failed"),
        requests.RequestException("request failed"),
    ],
)
def test_request_failure_stops_without_success_summary(
    error: requests.RequestException,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    first_response = make_response(iter([b"completed"]))
    session.get.side_effect = [first_response, error]
    monkeypatch.setattr(
        speed_test.time, "perf_counter", Mock(side_effect=[0.0, 1.0, 2.0])
    )

    assert speed_test.main([URL]) == 1

    assert session.get.call_count == 2
    first_response.__exit__.assert_called_once()
    session.__exit__.assert_called_once()
    output = capsys.readouterr()
    assert "Запрос 01/10" in output.out
    assert "Запрос 02/10" not in output.out
    assert "Итоги:" not in output.out
    assert str(error) in output.err
    assert "Тест не завершён" in output.err


@pytest.mark.parametrize("status_code", [404, 500])
def test_http_error_closes_response_and_session(
    status_code: int, session: MagicMock, capsys: pytest.CaptureFixture
) -> None:
    response = make_response(iter([b"error body"]), status_code)
    response.raise_for_status.side_effect = requests.HTTPError(
        f"HTTP {status_code}", response=response
    )
    session.get.return_value = response

    assert speed_test.main([URL]) == 1

    session.get.assert_called_once()
    response.iter_content.assert_not_called()
    response.__exit__.assert_called_once()
    session.__exit__.assert_called_once()
    output = capsys.readouterr()
    assert "Итоги:" not in output.out
    assert f"HTTP {status_code}" in output.err


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_redirect_is_an_error_without_extra_requests(
    status_code: int, session: MagicMock, capsys: pytest.CaptureFixture
) -> None:
    response = make_response(iter([]), status_code)
    session.get.return_value = response

    assert speed_test.main([URL]) == 1

    session.get.assert_called_once()
    assert session.get.call_args.kwargs["allow_redirects"] is False
    response.iter_content.assert_not_called()
    response.__exit__.assert_called_once()
    session.__exit__.assert_called_once()
    output = capsys.readouterr()
    assert "Итоги:" not in output.out
    assert "конечный URL" in output.err


@pytest.mark.parametrize(
    "error",
    [
        requests.ReadTimeout("body read timeout"),
        requests.ConnectionError("body connection lost"),
        requests.exceptions.ChunkedEncodingError("incomplete body"),
    ],
)
def test_body_read_failure_is_not_a_completed_request(
    error: requests.RequestException,
    session: MagicMock,
    capsys: pytest.CaptureFixture,
) -> None:
    def broken_body() -> Iterator[bytes]:
        yield b"partial"
        raise error

    response = make_response(broken_body())
    session.get.return_value = response

    assert speed_test.main([URL]) == 1

    session.get.assert_called_once()
    response.__exit__.assert_called_once()
    session.__exit__.assert_called_once()
    output = capsys.readouterr()
    assert "Запрос " not in output.out
    assert "Итоги:" not in output.out
    assert str(error) in output.err


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        URL,
        "HTTPS://example.com/file?q=1#fragment",
        "http://localhost:8000/file",
        "http://127.0.0.1/file",
        "http://[::1]:8000/file",
    ],
)
def test_valid_url(url: str) -> None:
    assert speed_test.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com/file",
        "ftp://example.com/file",
        "file:///tmp/file",
        "https://",
        "https:///file",
        "https://:443/file",
        "https://example.com:abc/file",
        "https://example.com:65536/file",
        "https://example.com:0/file",
        "https://[::1/file",
        "https://exa mple.com/file",
        " https://example.com/file",
        "https://example.com/\nfile",
    ],
)
def test_invalid_url_never_opens_session(
    url: str, session: MagicMock, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        speed_test.validate_url(url)
    with pytest.raises(SystemExit) as error:
        speed_test.main([url])

    assert error.value.code == 2
    session.__enter__.assert_not_called()
    session.get.assert_not_called()
    assert "HTTP/HTTPS URL" in capsys.readouterr().err


def test_missing_url_is_a_usage_error(
    session: MagicMock, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as error:
        speed_test.main([])

    assert error.value.code == 2
    session.__enter__.assert_not_called()
    assert "url" in capsys.readouterr().err
