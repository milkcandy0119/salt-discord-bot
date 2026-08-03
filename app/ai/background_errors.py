"""背景付費流程使用的可分類錯誤。"""


class BackgroundBudgetDeferred(RuntimeError):
    """表示額度不足，工作必須保留而不能視為失敗。"""


class RetryableBackgroundError(RuntimeError):
    """表示請求未計費或內部暫時錯誤，可安全退避重試。"""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class PermanentBackgroundError(RuntimeError):
    """表示自動重試可能重複計費或資料本身無法安全處理。"""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)
