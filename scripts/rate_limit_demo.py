"""Show the rate limit on POST /quests working (standard library only).

1. Sends limit + 3 requests as fast as possible: the first ones are accepted
   (201), the extra ones are rejected (429) with a Retry-After header.
2. Waits for the number of seconds Retry-After asked for.
3. Sends one more request, which is accepted again.

Run from the project folder while the app is running:
    python scripts/rate_limit_demo.py
"""
import argparse
import json
import time
import urllib.error
import urllib.request


def post_quest(base_url):
    """Returns (status, headers, body) for one POST /quests."""
    request = urllib.request.Request(base_url + "/quests", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.headers, json.loads(error.read())


def describe(number, status, headers, body):
    if status == 201:
        return f"#{number:>2}  {status}  accepted   RateLimit-Remaining={headers.get('RateLimit-Remaining')}"
    return f"#{number:>2}  {status}  rejected   Retry-After={headers.get('Retry-After')}s  ({body['message']})"


def main():
    parser = argparse.ArgumentParser(description="Rate limit demo for POST /quests")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    # The first response tells us the limit, so the demo works for any setting.
    status, headers, body = post_quest(args.base_url)
    if status != 201:
        print(f"First request was not accepted ({status}). Wait for the window to end and try again.")
        return
    limit = int(headers["RateLimit-Limit"])
    print(f"Limit: {limit} requests per window. Sending {limit + 3} requests quickly...\n")
    print(describe(1, status, headers, body))

    retry_after = None
    for number in range(2, limit + 4):
        status, headers, body = post_quest(args.base_url)
        print(describe(number, status, headers, body))
        if status == 429:
            retry_after = int(headers["Retry-After"])

    if retry_after is None:
        print("\nNo request was rejected (the window may have reset during the run).")
        return

    print(f"\nWaiting {retry_after}s, as Retry-After asked...\n")
    time.sleep(retry_after)
    status, headers, body = post_quest(args.base_url)
    print("After waiting: " + describe(limit + 4, status, headers, body))


if __name__ == "__main__":
    main()
