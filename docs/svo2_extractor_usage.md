# SVO2画像抽出プログラム 利用方法

## 必要な環境

- ZED SDKおよびPython API（`pyzed`）
- NVIDIA GPUと、ZED SDKに対応するドライバー
- Python 3.10以降
- NumPy
- Pillow
- PyYAML

本プログラムはOpenCVを使用しない。現在の環境にあるNumPyとOpenCVのABI不整合は、抽出プログラムの実行には影響しない。

## ファイル

- `scripts/svo_extract.py`: 抽出CLI
- `configs/extract.example.yaml`: 設定例
- `docs/svo2_extractor_design.md`: 設計方針
- `tests/test_svo_extract.py`: GPUを使用しない単体テスト

## 基本的な流れ

### 1. SVO2を確認する

```bash
python3 scripts/svo_extract.py inspect .
```

既定では次のパターンを探索する。

```text
*/*/recording.svo2
```

特定のSVO2だけを確認する例：

```bash
python3 scripts/svo_extract.py inspect . \
  --pattern '20260611-12Ehime/20260611_105930/recording.svo2'
```

### 2. 設定ファイルを用意する

`configs/extract.example.yaml`を参考に設定する。初回は出力枚数を少なくして動作確認することを推奨する。

主な設定：

```yaml
export:
  name: candidates_v1
  existing: error

sampling:
  method: stratified_random
  count_per_svo: 100
  seed: 42

outputs:
  left:
    enabled: true
    format: jpg
    quality: 95
  right:
    enabled: true
    format: jpg
    quality: 95
  depth:
    enabled: true
    format: png16
    unit: millimeter

zed:
  depth_mode: NEURAL_LIGHT

progress:
  interval_seconds: 5
```

### 3. 抽出計画を作成する

```bash
python3 scripts/svo_extract.py plan --config configs/extract.example.yaml
```

画像はまだ生成されない。各セッションについて次のファイルが作成される。

```text
<セッションID>_exports/<実行名>/manifest.csv
<セッションID>_exports/<実行名>/config.yaml
```

### 4. 抽出する

```bash
python3 scripts/svo_extract.py extract --config configs/extract.example.yaml
```

計画がない場合に自動作成するには次を使用する。

```bash
python3 scripts/svo_extract.py extract \
  --config configs/extract.example.yaml \
  --plan-if-missing
```

中断後に再開する場合：

```bash
python3 scripts/svo_extract.py extract \
  --config configs/extract.example.yaml \
  --resume
```

抽出中は、既定で5秒間隔と処理終了時に進捗が表示される。

```text
  progress 37/100 ( 37.0%) frame=1492 completed=37 failed=0 elapsed=00:18
  progress 74/100 ( 74.0%) frame=2981 completed=74 failed=0 elapsed=00:36
  progress 100/100 (100.0%) frame=4012 completed=100 failed=0 elapsed=00:49
```

表示頻度は設定で変更できる。`0`を指定すると、抽出フレームごとに表示する。

```yaml
progress:
  interval_seconds: 10
```

この値は表示だけに影響し、既存manifestとの設定整合性判定には使用されない。

### 5. 結果を検証する

```bash
python3 scripts/svo_extract.py verify \
  20260611-12Ehime/20260611_104824_exports/candidates_v1
```

左右画像と深度の存在、画像の読み込み可否、解像度の一致を検査する。

## 出力例

```text
20260611-12Ehime/
├── 20260611_104824/
│   └── recording.svo2
└── 20260611_104824_exports/
    └── candidates_v1/
        ├── left/
        ├── right/
        ├── depth/
        ├── depth_preview/
        ├── manifest.csv
        └── config.yaml
```

入力セッションディレクトリには書き込まない。

## 抽出方式

設定可能な`sampling.method`：

- `stratified_random`: 動画全体を時間区間へ分けてランダム抽出。既定値
- `uniform_random`: 全有効フレームから単純ランダム抽出
- `interval`: 固定間隔で抽出
- `explicit`: `sampling.frames`で指定したフレームを抽出
- `existing`: 既存の`frames`に対応する右画像・深度を抽出

明示フレームの例：

```yaml
sampling:
  method: explicit
  frames: [100, 500, 1000]
```

既存画像に対応するフレームの例：

```yaml
sampling:
  method: existing
```

## 深度形式

### 16ビットPNG

```yaml
depth:
  enabled: true
  format: png16
  unit: millimeter
  invalid_value: 0
  max_depth_meters: 40
```

有効な深度値をmm単位で保存し、無効値を0とする。

### 32ビットNPY

```yaml
depth:
  enabled: true
  format: npy
  unit: meter
```

浮動小数点値とNaNを維持したい場合に使用する。

## 上書きと再開

`export.existing`には次を指定できる。

- `error`: 既存計画があれば停止。安全な既定値
- `resume`: 既存manifestを使用
- `skip`: 既存manifestを使用
- `overwrite`: manifestと設定スナップショットを作り直す

既存計画と現在の抽出・出力設定が異なる場合は停止する。設定を変更したい場合は、`export.name`を変更して別の出力として作成する。

## テスト

GPUを使用しない単体テスト：

```bash
python3 -m unittest discover -s tests -v
```

実SVO2を使う`inspect`と`extract`には、ZED SDKからGPUへアクセスできる実行環境が必要である。
