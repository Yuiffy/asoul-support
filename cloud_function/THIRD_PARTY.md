# Sources

- X25Kn protocol and HMAC chain: [XiaoYiWeio/asoul-support](https://github.com/XiaoYiWeio/asoul-support),
  `scripts/heartbeat.py`, commit `b52664ccc8389992acb8335422a760e4d8cf1c66` (v4.1.1).
  The upstream README identifies MIT licensing; that snapshot has no separate LICENSE file.
  This adapter retains attribution and changes transport injection and scheduling.
- Daily task schema, WBI signing and current task verification:
  [andywang425/BLTH](https://github.com/andywang425/BLTH), MIT, copyright (c) 2023 andywang425.
- Gift request fields checked against `bilibili-api-python` 17.4.2, `LiveRoom.send_gift_gold`.
- OBS V2 request signing and append semantics checked against Huawei's
  `esdk-obs-python` 3.26.6 (`obs/auth.py`, `obs/client.py`). No SDK is bundled.

## BLTH MIT notice

Copyright (c) 2023 andywang425

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
