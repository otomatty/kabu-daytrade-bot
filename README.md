# dtbot — kabuステーションAPI 対応のデイトレード自動売買ボット

日本株のデイトレードを自動で行うボットです。三菱UFJ eスマート証券の **kabuステーション®API** で発注し、
ブラウザで動きを確認・操作できる **ダッシュボード** が付いています。

同じ売買ロジックを、次の4つのモードでそのまま動かせます。

| モード | 時価 | 約定 | 用途 |
| --- | --- | --- | --- |
| `demo` | 合成データ | シミュレーション | 口座なしで動きとダッシュボードを確かめる |
| `backtest` | 保存した過去データ | シミュレーション | 戦略を検証する |
| `paper` | kabuステーションの本物の時価 | シミュレーション | 本番前の試運転 |
| `live` | kabuステーションの本物の時価 | **本物の発注** | 本番 |

> **ご注意**
> このソフトウェアは利益を保証しません。同梱の戦略(オープニングレンジ・ブレイクアウト)は仕組みを確かめるためのサンプルです。
> 自動売買では、プログラムの不具合・通信の障害・相場の急変で損失が出ることがあります。
> 少額で十分に試し、ご自身の判断と責任で使ってください。見せ玉など相場操縦にあたる発注をしないでください。

---

## 1. 必要なもの

- Windows 10 / 11(kabuステーションが Windows 専用のため。開発や `demo`・`backtest` は Mac や Linux でも動きます)
- Python 3.11 以上(3.11 / 3.12 / 3.13 で動作を確認しています)
- 本番とペーパーでは:
  - 三菱UFJ eスマート証券の口座と、kabuステーション®(API を無料で使うには Professional プラン以上)
  - kabuステーションの API 利用設定(ログイン後の画面で「kabuステーション®API」から設定)
  - デイトレードで空売りもするなら信用取引口座

## 2. インストール

[uv](https://docs.astral.sh/uv/) を使うのがおすすめです(PowerShell で実行)。

```powershell
# uv をまだ入れていない場合
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# リポジトリを取得して、依存パッケージを入れる
git clone https://github.com/otomatty/kabu-daytrade-bot.git
cd kabu-daytrade-bot
uv sync
```

(開発やテストをしないなら `uv sync --no-dev` で、テスト用のパッケージを省けます)

pip を使う場合は `python -m venv .venv` → `.venv\Scripts\activate` → `pip install -e .` です。
以降の `dtbot` は、uv なら `uv run dtbot ...` として実行してください。

## 3. まず動かしてみる(口座不要)

```powershell
uv run dtbot init      # config\settings.yaml と .env のひな形を作る
uv run dtbot demo      # 合成データで30倍速で動かし、ブラウザでダッシュボードを開く
```

コンソールに `ダッシュボード: http://127.0.0.1:8765/#token=...` と表示されます。
この URL にはトークンが含まれるので、他の人に見せないでください。

## 4. 本番までの進め方

1. **kabuステーションの準備**: kabuステーションを起動してログインし、「システム設定 > API」で API を有効にして
   APIパスワードを設定します。`.env` の `KABU_API_PASSWORD=` にそのパスワードを書きます。
2. **接続の確認**: `uv run dtbot check` で、トークンの発行・余力・時価の取得ができるか確かめます。
3. **データを貯める**: 取引時間中に `uv run dtbot collect` を動かすと、ティックが `data\dtbot.sqlite3` に保存されます。
   ほかで手に入れた1分足の CSV があれば `uv run dtbot import-bars bars.csv --symbol 7203` で取り込めます。
   (CSV の列: `datetime,open,high,low,close,volume`。時刻は足の始まりの日本時間)
4. **バックテスト**: `uv run dtbot backtest --from 2026-10-01 --to 2026-10-31`
   (収集したティックを使うなら `--source ticks`。パラメータを変えて試すなら `--param range_minutes=30`)。
   結果は `uv run dtbot dashboard` の「バックテスト」で見られます。
5. **ペーパートレード**: `uv run dtbot paper` で、本物の時価に対して約定だけをシミュレーションします。
   2〜4週間ほど動かし、バックテストとの差を確かめてください。
6. **リハーサル(任意)**: `uv run dtbot mock-kabu` で kabuステーションAPI のモックを起動し、
   設定の `kabu.port` を `18090` にすると、`live` の流れを本物の発注なしで試せます。
   kabuステーションの検証用の環境(`kabu.environment: verification`、ポート18081)も使えます。
7. **本番**: 設定の `trading.live_enabled` を `true` にし、`uv run dtbot live --confirm-live` で起動します。
   最初は `sizing.qty` を最小単位(100株)にし、`risk.max_daily_loss` を小さくして始めてください。

### 毎日の運用で気をつけること

- kabuステーションは**早朝に強制ログアウト**します。毎朝、寄り付き前にログインし直してください
  (ボットはトークンを自動で発行し直します)。
- 止めるときは、ダッシュボードの「全決済する」を押して建玉が無くなったのを確かめてから、
  コンソールで Ctrl+C を押します。建玉を残したまま止めると、ログに警告が出ます。
- パソコンのスリープ・Windows Update の再起動で止まらないように設定してください。慣れてきたら Windows の VPS が安定します。

## 5. ダッシュボード

- **状態の帯**: 稼働中 / 新規停止中 / 緊急停止(帯全体が赤くなります)と、その理由。
- **操作**: 新規エントリーを止める・再開する、全決済する、緊急停止(確認の画面が出ます)。
- **立会の時間軸**: 前場・昼休み・後場と現在時刻、新規締切(15:10)と手仕舞い(15:20)の位置。
- **本日の損益**: 損失上限(緊急停止の線)まであとどれだけあるか。
- **値動き**: 1分足と約定の位置。色は日本のチャートの慣習どおり、**赤が上昇・買い、青が下落・売り**です。
- **建玉・注文・本日の取引・記録**、**バックテスト**の結果(累積損益、日ごとの損益、取引の一覧)。

安全のため、ダッシュボードは `127.0.0.1` だけで待ち受け、トークンの無い操作・他のサイトからの操作を拒否します。

## 6. 安全装置

| 仕組み | 内容 |
| --- | --- |
| 戦略は発注しない | 戦略はシグナルを出すだけ。数量の決定と発注の可否はリスク管理が判断する |
| 日次損失上限 | 確定+評価損益が `risk.max_daily_loss` に達したら緊急停止し、全建玉を成行で決済 |
| 建玉の上限 | 銘柄ごと・合計の金額、同時に持つ銘柄数、1注文の株数 |
| 発注の回数制限 | 1分あたりの新規注文の上限(暴走防止)。API の流量制限(秒間10件)も守る |
| 時間の制限 | 新規は `entry_cutoff` まで。`flatten_at` に全建玉を成行で手仕舞い(15:25 より前) |
| 連続エラー | 発注エラーが続いたら緊急停止 |
| 時価の途絶 | 場中に時価配信が `stale_data_sec` 秒届かなければ新規を止め、復旧したら再開 |
| 証券会社との照合 | 定期的に注文・建玉を照合。2回続けて合わなければ緊急停止し、証券会社の建玉に合わせて決済 |
| 二重発注の防止 | 応答が途切れた発注は自動で再送せず、照合で受付を確認する。約定は約定番号で重複を除く |
| 再起動 | 当日の約定の記録と証券会社の建玉から状態を戻す。起動前の約定を二重に数えない |
| 既存の建玉を守る | 信用は、起動した時点で前日以前からある建玉を除外する。現物は、起動時の保有株から当日の約定を引いた数を「元から持っていた株」として除外する(長期保有の株を売らない) |

## 7. 主な設定(`config\settings.yaml`)

設定ファイルの各項目にコメントがあります。主なものは次のとおりです。秘密情報は `.env` に書きます。

- `universe`: 取引する銘柄(最大50)。TOPIX500 の銘柄は `tick_table: topix500`(呼値が細かい)
- `strategy`: 戦略の名前とパラメータ
- `sizing`: 1回の売買の金額(`fixed_notional`)か株数(`fixed_qty`)
- `trading`: 信用/現物、信用の種類(制度・一般・デイトレ)、口座種別、新規注文の市場(東証+ / SOR)
- `risk`, `session`: 上の表の安全装置と時刻
- `fees`, `simulation`: バックテストとペーパーの手数料・スリッページ

呼値の表(`core/ticksize.py`)と取引時間(`core/session.py`)は東証の規則に合わせています。
規則の変更がありえるので、運用前に日本取引所グループの最新の情報と照らし合わせてください。

## 8. 自分の戦略を作る

`Strategy` を継承し、`on_bar`(足の確定ごと)や `on_tick`(ティックごと)でシグナルを返します。

```python
from collections.abc import Sequence
from daytrade_bot.core.models import Bar, Signal, SignalAction
from daytrade_bot.core.strategy import Strategy, StrategyContext, StrategyParams


class MyParams(StrategyParams):
    threshold: float = 0.5


class MyStrategy(Strategy[MyParams]):
    name = "my_strategy"
    Params = MyParams

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        if ctx.position(bar.symbol) == 0 and ctx.entries_allowed():
            return [Signal(bar.symbol, SignalAction.ENTER_LONG, reason="条件を満たした")]
        return []
```

設定で `strategy.name: mypackage.mymodule:MyStrategy` と指定します。数量を省くと `sizing` の設定で決まり、
手仕舞い(`EXIT`)は全数量になります。同梱の `strategies/orb.py` が実例です。

## 9. 構成

```
src/daytrade_bot/
  core/          共通コア(どのモードでも同じコード)
    engine.py      イベントキューで時価・注文の更新・操作を順に処理する
    risk.py        上限とキルスイッチ
    order_manager.py 注文の状態機械と照合
    portfolio.py   建玉と損益
    session.py     取引日と立会時間 / ticksize.py 呼値 / bars.py 足の生成
  strategies/    戦略(orb.py)
  brokers/       sim.py(シミュレーション)、kabu/(kabuステーションAPI)
  feeds/demo.py  デモ用の合成データ
  backtest/      バックテストと指標
  dashboard/     ダッシュボード(FastAPI + WebSocket + 画面)
  devtools/mock_kabu.py  kabuステーションAPI のモック
  storage/db.py  SQLite(ティック、足、注文、約定、記録、バックテスト結果)
```

## 10. 開発と検証

```powershell
uv run pytest            # テスト(本物のブラウザを使う画面のテストは、Chrome が無ければ飛ばす)
uv run pytest --cov      # 網羅率(90%未満なら失敗)
uv run mypy              # 型チェック(strict)
uv run ruff check        # リント
```

売買の判断・注文の管理・kabuステーションAPI のアダプタ・ダッシュボードの安全対策は、
作者とは別のレビューでも確認し、見つかった不具合はすべて回帰テストを付けて直しています。

kabuステーションAPI のアダプタは、モックのサーバーに実際の HTTP と WebSocket でつないで検証し、
ボットが送ったすべてのリクエストを公式の仕様(`tests/fixtures/kabusapi/kabu_STATION_API.yaml`)で検査しています。
本物の kabuステーションでの動作は、作者の環境では確認できていません。`check` → `paper` → 少額の `live` の順に確かめてください。

## 11. 分かっている制約

- シミュレーションでは部分約定を扱いません(全数量が一度に約定します)。1分足のバックテストでは足の中の値動きを近似します。
- 逆指値・IOC など、成行と指値以外の執行条件には対応していません。
- 先物・オプション・米国株には対応していません(現物株と信用取引のみ)。
- kabuステーションの注文状態は約1秒ごとの照会で取得するため、約定の反映に少し遅れがあります。
- 日をまたいで建玉を持ち越す運用は想定していません。
- 時価の配信が途絶えている間は新規エントリーを止めますが、持っている建玉はそのままです(ティックで判定する損切りも働きません)。
  途絶えが続くときは、ダッシュボードの「全決済する」を使ってください。15:20 の強制手仕舞いは時価が無くても行います。
- 現物取引(`account_mode: cash`)では、対象の銘柄を同じ日に手動で売買しないでください(ボットの売買と区別できません)。
  信用取引でも、ボットが動いている日に対象の銘柄で手動の建玉を作ると、照合で不一致になり緊急停止します。

## ライセンス

- このソフトウェア: MIT License
- チャート: [TradingView Lightweight Charts™](https://www.tradingview.com/)(Apache License 2.0、`dashboard/static/vendor/LICENSE-lightweight-charts.txt`)
- `tests/fixtures/kabusapi/`: 三菱UFJ eスマート証券の公式リポジトリの仕様ファイル(MIT License)
