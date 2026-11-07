"""IAM-style glob matching shared by keymaster and sentinel."""

from __future__ import annotations

from functools import cache


def glob_intersects(a: str, b: str) -> bool:
    """True when some action name matches both IAM patterns (``*`` and ``?``; any case).

    Checked both ways at once, so ``iam:*`` vs ``iam:PassRole`` and ``s3:*`` vs
    ``s3:DeleteBucket`` both intersect, and so do ``s3:Get*`` vs ``s3:*Object``.
    """
    x, y = a.lower(), b.lower()

    @cache
    def go(i: int, j: int) -> bool:
        if i == len(x):
            return all(c == "*" for c in y[j:])
        if j == len(y):
            return all(c == "*" for c in x[i:])
        if x[i] == "*":
            return go(i + 1, j) or go(i, j + 1)
        if y[j] == "*":
            return go(i, j + 1) or go(i + 1, j)
        return (x[i] == y[j] or "?" in (x[i], y[j])) and go(i + 1, j + 1)

    return go(0, 0)
