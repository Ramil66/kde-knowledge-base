---
repo: kio
external_id: 2214
title: Preserve file name errors
source_repository_url: https://invent.kde.org/example/kio
source_branch: fix/file-name-too-long
target_branch: master
discussions:
  - external_id: thread-1
    comments:
      - external_id: note-1
        body: "Please cover the filename boundary in a test."
        author:
          platform: invent
          username: example-user
        position:
          new_path: src/example.cpp
          new_line: 42
          head_sha: 0123456789abcdef0123456789abcdef01234567
---
Preserve the original error code when a file name exceeds the limit.
