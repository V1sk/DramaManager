"""Per-developer object-storage credentials — TEMPLATE.

This file IS committed. The real credentials live in `credentials.py`
(same directory), which is gitignored so each developer holds their own
keys ("按人分配").

Setup on a fresh clone:

    cp app/storage/credentials.example.py app/storage/credentials.py
    # then fill in the AccessKey id/secret pair issued to you

Only secret keys live here. Endpoint / bucket / region are not secrets and
stay in oss_provider.py / tos_provider.py. You only need to fill in the keys
for the provider you actually use (`STORAGE_PROVIDER=oss` or `tos`);
`STORAGE_PROVIDER=none` needs neither.
"""

# Aliyun OSS — required when STORAGE_PROVIDER=oss
OSS_ACCESS_KEY_ID = ""
OSS_ACCESS_KEY_SECRET = ""

# Volcengine TOS — required when STORAGE_PROVIDER=tos
TOS_ACCESS_KEY = ""
TOS_SECRET_KEY = ""
