"""
ai_stub.py - タスク割り当てAI (WariAthena) の差し込み口

ここは雛形です。LinUCB / Thompson Sampling の実装はこのファイルを
置き換える形で行ってください。app.py からは assign_task() だけが
呼ばれるので、この関数のシグネチャを保てば内部は自由に変更できます。

想定する文脈ベクトル x:
    - worker["years_of_service"] : 勤続年数（事前登録の1項目）
    - task["difficulty"]         : タスク難易度 1-5
    - work_logs から集計した過去の所要時間実績（報酬 r の計算元）

報酬 r = 全従業員の同タスク平均所要時間 / 今回の実所要時間
"""


def assign_task(task: dict, workers: list[dict], work_logs: list[dict]) -> int | None:
    """
    タスクに最適な作業者の worker_id を返す。候補がいなければ None。

    現状はダミー実装:
      「担当中タスクが少ない人を優先し、同数なら勤続年数が長い人」
    ここを LinUCB に置き換える。
    """
    if not workers:
        return None
    # ダミー: 勤続年数が最長の作業者を返すだけ
    best = max(workers, key=lambda w: w["years_of_service"])
    return best["id"]


def update_model(task: dict, worker_id: int, duration_sec: int, work_logs: list[dict]) -> None:
    """
    タスク完了時に呼ばれる学習フック。
    A ← A + x x^T,  b ← b + r x  の更新をここに実装する。
    現状は何もしない。
    """
    pass
