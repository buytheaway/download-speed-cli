"""Measure response body download speed with ten sequential HTTP requests."""

import argparse
import sys
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import requests


REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024
TIMEOUT = (5.0, 30.0)


def validate_url(value: str) -> str:
    """Validate an HTTP(S) URL without making a network request."""
    message = "Укажите HTTP/HTTPS URL с корректным хостом и портом."
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.port == 0
            or any(character.isspace() for character in value)
        ):
            raise ValueError(message)
    except ValueError as error:
        raise argparse.ArgumentTypeError(message) from error
    return value


def calculate_speed(byte_count: int, elapsed_seconds: float) -> float:
    """Return Mbps using bytes read and the full request duration."""
    return byte_count * 8 / elapsed_seconds / 1_000_000


def download_once(session: requests.Session, url: str) -> tuple[int, float]:
    """Read a response in chunks and return decoded body bytes and seconds."""
    byte_count = 0
    started_at = time.perf_counter()
    with session.get(
        url,
        stream=True,
        headers={"Accept-Encoding": "identity"},
        timeout=TIMEOUT,
        allow_redirects=False,
    ) as response:
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError(
                f"Получен HTTP {response.status_code}; ожидается ответ 2xx. "
                "При перенаправлении укажите конечный URL.",
                response=response,
            )
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            byte_count += len(chunk)
    elapsed_seconds = time.perf_counter() - started_at
    return byte_count, elapsed_seconds


def run_speed_test(url: str) -> list[tuple[int, float]]:
    """Run ten sequential downloads, printing each completed measurement."""
    results: list[tuple[int, float]] = []
    with requests.Session() as session:
        for request_number in range(1, REQUEST_COUNT + 1):
            byte_count, elapsed_seconds = download_once(session, url)
            results.append((byte_count, elapsed_seconds))
            speed = calculate_speed(byte_count, elapsed_seconds)
            print(
                f"Запрос {request_number:02d}/{REQUEST_COUNT}: "
                f"{elapsed_seconds:.3f} с | {byte_count} байт | "
                f"{speed:.3f} Mbps"
            )
    return results


def print_summary(results: list[tuple[int, float]]) -> None:
    """Print aggregate statistics for a completed test."""
    total_bytes = sum(byte_count for byte_count, _ in results)
    total_seconds = sum(seconds for _, seconds in results)
    average_seconds = total_seconds / len(results)
    average_speed = calculate_speed(total_bytes, total_seconds)
    print(f"\nИтоги: {len(results)} успешных запросов")
    print(f"Среднее время запроса: {average_seconds:.3f} с")
    print(f"Общий объём: {total_bytes} байт")
    print(f"Суммарное время запросов: {total_seconds:.3f} с")
    print(f"Средняя скорость: {average_speed:.3f} Mbps")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Скорость скачивания тела ответа: 10 последовательных GET."
    )
    parser.add_argument("url", type=validate_url, help="HTTP/HTTPS URL файла")
    args = parser.parse_args(argv)

    try:
        results = run_speed_test(args.url)
    except requests.RequestException as error:
        print(f"Ошибка запроса: {error}", file=sys.stderr)
        print(
            "Тест не завершён; итоговая статистика не рассчитана.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nТест прерван пользователем.", file=sys.stderr)
        return 130

    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
