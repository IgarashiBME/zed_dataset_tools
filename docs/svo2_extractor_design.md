# SVO2画像抽出プログラム 設計方針

## 1. 目的

本プログラムは、ZEDカメラで記録したSVO2ファイルから、指定した一部のフレームについて以下のデータを抽出する。

- 左カメラ画像
- 右カメラ画像
- 左カメラ画像に対応する深度データ
- 任意で深度確認用プレビュー画像

全フレームを抽出せず、抽出枚数や抽出方法を指定できるようにする。主な用途は、後段でセマンティックセグメンテーション用データセットを作成するための候補素材生成である。

本プログラムの責務はSVO2からの抽出までとし、画像の選別やデータセット分割は別プログラムで行う。

## 2. 責務の境界

### 2.1 本プログラムが担当すること

- 現在のディレクトリ構成から`recording.svo2`を発見する
- 抽出対象フレームを再現可能な方法で決定する
- 左画像、右画像、深度データを同一フレームから取得する
- 指定された画像形式で保存する
- 抽出条件と抽出結果をmanifestへ記録する
- 中断した処理を再開する
- 出力ファイルの整合性を検証する
- 入力データとは別のexportsディレクトリへ出力する

### 2.2 本プログラムが担当しないこと

- 不要画像の人手による除外
- ブレ、重複、露出不良などの自動除外
- small、medium、largeデータセットの作成
- データセット間の包含関係の管理
- train、validation、testへの分割
- セマンティックセグメンテーションラベルの管理
- アノテーションツールとの連携
- クラスや撮影シーンの分布調整
- 学習用ディレクトリへのコピーやハードリンク作成

抽出結果は完成済みデータセットではなく、後段で選別・整形するための候補素材として扱う。

## 3. 現在の入力構成

現在の構成は、撮影グループの下に収録セッションがあり、各セッション内にSVO2と付随ファイルが置かれている。

```text
20260611-12Ehime/
├── 20260611_102930/
│   ├── recording.svo2
│   ├── meta.json
│   ├── imu.csv
│   ├── frames/
│   └── labels/
├── 20260611_104824/
└── 20260611_105930/
```

この構成は今後も維持される前提とする。既存の`frames`と`labels`は本プログラムから変更しない。

SVO2の探索パターンは、既定で次の形に限定する。

```text
<入力ルート>/<撮影グループ>/<セッション>/recording.svo2
```

glob表現では次のようになる。

```text
*/*/recording.svo2
```

無制限な再帰探索を避けることで、出力ディレクトリや意図しないSVO2の誤検出を防ぐ。

## 4. 出力構成

元のSVO2と生成物を明確に分けるため、出力はセッションディレクトリの内部ではなく、同じ階層に新しいディレクトリとして作成する。

入力が次の場合、

```text
20260611-12Ehime/20260611_104824/recording.svo2
```

出力ルートは次のようにする。

```text
20260611-12Ehime/20260611_104824_exports/
```

抽出設定の混在を防ぐため、出力ルートの下に実行名を置く。

```text
20260611-12Ehime/
├── 20260611_104824/
│   ├── recording.svo2
│   ├── meta.json
│   ├── imu.csv
│   ├── frames/
│   └── labels/
│
└── 20260611_104824_exports/
    └── candidates_v1/
        ├── left/
        ├── right/
        ├── depth/
        ├── depth_preview/
        ├── manifest.csv
        └── config.yaml
```

出力ルートは、SVO2が存在するセッションディレクトリから機械的に決定できる。

```python
session_dir = svo_path.parent
export_root = session_dir.with_name(f"{session_dir.name}_exports")
```

入力側は読み取り専用として扱い、本プログラムからファイルを作成・更新・削除しない。

## 5. ファイル名

左画像、右画像、深度には同じ画像IDを使用する。

```text
left/20260611_104824_004480.jpg
right/20260611_104824_004480.jpg
depth/20260611_104824_004480.png
depth_preview/20260611_104824_004480.png
```

画像IDは次の構成とする。

```text
<セッションID>_<SVOフレーム番号6桁>
```

この命名方法は、現在の`frames`ディレクトリにある画像の命名規則と一致し、元SVO2のフレームを追跡しやすい。

## 6. フレーム抽出方法

### 6.1 既定方式

既定の抽出方式は`stratified_random`（層化ランダム）とする。

1. 有効な録画範囲を抽出枚数と同じ数の時間区間へ分割する
2. 各区間から1フレームをランダムに選ぶ
3. 最小フレーム間隔を満たすように調整する
4. 抽出対象をフレーム番号の昇順に並べる

これにより、動画全体をカバーしながらランダム性を維持し、近接した類似フレームが過度に選ばれることを抑える。

### 6.2 対応する抽出方式

| 方式 | 説明 | 主な用途 |
|---|---|---|
| `stratified_random` | 時間軸を分割し、各区間からランダム抽出 | 通常の候補画像生成。既定値 |
| `uniform_random` | 有効範囲の全フレームから単純ランダム抽出 | 完全な一様乱数が必要な場合 |
| `interval` | 一定フレーム数または一定秒数ごとに抽出 | 定期サンプリング |
| `explicit` | 指定されたフレーム番号を抽出 | 再抽出や調査 |
| `existing` | 既存の`frames`のファイル名からフレーム番号を取得 | 既存左画像に対応する右・深度の生成 |

### 6.3 抽出範囲

録画開始直後と終了直前を除外できるようにする。

```yaml
sampling:
  start_margin_seconds: 5
  end_margin_seconds: 5
  min_gap_seconds: 0
```

必要に応じて、開始・終了時刻または開始・終了フレームを直接指定できるようにする。

### 6.4 抽出枚数

基本単位はSVO2ごとの指定枚数とする。

```yaml
sampling:
  count_per_svo: 700
```

50枚、200枚、500枚など任意の枚数を指定可能にする。ただし、それらをsmall、medium、largeデータセットへ分ける処理は本プログラムでは行わない。

最終的に500枚を採用したい場合、後段での除外を見込んで650～750枚程度を候補として抽出する、といった運用は利用者側で決める。

## 7. 再現性と抽出計画

乱数シードを設定し、同じ入力と同じ設定から同じフレームを選択できるようにする。

```yaml
sampling:
  method: stratified_random
  count_per_svo: 700
  seed: 42
```

抽出は「計画」と「実行」に分離する。

1. SVO2の総フレーム数、FPS、解像度などを取得する
2. 抽出対象フレームを決定する
3. manifestへ`pending`として保存する
4. manifestに従って画像を抽出する
5. 成功した項目を`completed`へ変更する

manifestが既に存在する場合、それを抽出計画の正本とする。再開時に乱数を生成し直して対象フレームを変更してはならない。

再現性に影響する以下の情報を`config.yaml`へ保存する。

- 乱数シード
- 抽出方式
- 抽出アルゴリズムのバージョン
- 抽出枚数
- 抽出範囲
- 最小間隔
- 画像出力設定
- 深度設定

## 8. 抽出処理の高速化

抽出対象フレームをランダム順に処理すると、圧縮動画のシーク負荷が増える可能性がある。そのため、対象フレーム番号は抽出計画の作成後に昇順へ並べて処理する。

### 8.1 `seek`方式

対象フレームごとにZED SDKの`set_svo_position()`で移動する。

- 抽出率が非常に低い場合に有利
- 対象フレーム同士が大きく離れている場合に向く
- 圧縮方式やキーフレーム間隔によっては、シークのたびに復号コストが発生する

### 8.2 `scan`方式

先頭から順番に`grab()`する。ただし未選択フレームでは以下を行わない。

- 深度計算
- 左右画像の取得
- CPUへの画像転送
- ファイル書き込み

ZED SDKの`RuntimeParameters.enable_depth`を用いて、選択されていないフレームの深度計算を無効化する。

### 8.3 `hybrid`方式

次の抽出対象までの距離に応じて`seek`と`scan`を切り替える。

```text
次の対象が近い → scan
次の対象が遠い → seek
```

既定方式は`hybrid`とする。

```yaml
playback:
  strategy: hybrid
  seek_threshold_frames: 120
```

抽出の進捗は一定時間ごとに表示する。表示には処理済み件数、総件数、割合、現在の対象フレーム、成功・失敗件数、経過時間を含める。既定の更新間隔は5秒とし、設定で変更可能にする。

```yaml
progress:
  interval_seconds: 5
```

適切な閾値はSVO2の圧縮方式、GPU、ストレージ性能に依存するため、少数フレームで`seek`、`scan`、`hybrid`を比較できるベンチマーク機能を将来的に用意する。

### 8.4 書き込み処理

ZED SDKによる再生と深度計算は基本的に1本の処理として実行する。JPEGやPNGのエンコードと書き込みは、容量制限付きの小さなワーカーキューへ渡すことを検討する。

SVO2単位の並列処理はGPUメモリやデコーダーを競合させる可能性があるため、初期実装では行わない。

## 9. 出力形式

### 9.1 左・右画像

左画像と右画像は、それぞれ出力の有効・無効と形式を設定できるようにする。

初期実装で対応する形式は次の2つとする。

- JPEG
- PNG

JPEGでは品質を指定可能にする。

```yaml
outputs:
  left:
    enabled: true
    format: jpg
    quality: 95
  right:
    enabled: true
    format: jpg
    quality: 95
```

PNGでは圧縮レベルを指定可能にする。

```yaml
outputs:
  left:
    enabled: true
    format: png
    compression: 3
```

既定ではZED SDKによる補正済みの`VIEW.LEFT`と`VIEW.RIGHT`を取得する。

### 9.2 深度データ

深度の数値データと表示用画像を区別する。

初期実装では次の形式を対象とする。

#### 16ビットPNG

- 単位はmm
- 無効値は0
- 最大値は65,535mm
- 一般的な画像処理ライブラリで扱いやすい

```yaml
outputs:
  depth:
    enabled: true
    format: png16
    unit: millimeter
    invalid_value: 0
    max_depth_meters: 40
```

#### NumPy NPY

- 32ビット浮動小数点を維持できる
- NaNやInfを維持できる
- Pythonでの後処理に適する

```yaml
outputs:
  depth:
    enabled: true
    format: npy
    unit: meter
```

EXRは将来の追加候補とする。

### 9.3 深度プレビュー

目視確認用として、8ビットのグレースケールまたは疑似カラー画像を任意で出力する。

```yaml
outputs:
  depth_preview:
    enabled: false
    format: png
    colormap: turbo
```

深度プレビューは表示専用であり、距離データとして使用しない。

### 9.4 解像度

既定ではSVO2のネイティブ解像度を使用する。

```yaml
image:
  resolution: native
```

将来リサイズ出力に対応する場合、左、右、深度を同一解像度に揃え、リサイズ後の解像度をmanifestへ記録する。

## 10. 深度設定

深度計算に関する設定を設定ファイルから変更できるようにする。

```yaml
zed:
  depth_mode: NEURAL_LIGHT
  confidence_threshold: 50
  texture_confidence_threshold: 100
```

ZED SDK 5系で速度を優先する`NEURAL_LIGHT`を初期値とする。必要に応じて`NEURAL`、`NEURAL_PLUS`と品質・速度を比較する。深度値は左画像の座標系に対応するものを保存する。

## 11. manifest

`manifest.csv`を抽出計画と抽出結果の正本とする。

例：

```csv
sample_order,image_id,source_svo,requested_frame_index,actual_frame_index,timestamp_ns,left_path,right_path,depth_path,width,height,status,error
1,20260611_104824_004480,../20260611_104824/recording.svo2,4480,4480,1781140000000000000,left/20260611_104824_004480.jpg,right/20260611_104824_004480.jpg,depth/20260611_104824_004480.png,1280,720,completed,
```

最低限、次の項目を記録する。

- 抽出順
- 画像ID
- 入力SVO2の相対パス
- 要求したフレーム番号
- 実際に取得したフレーム番号
- SVO内タイムスタンプ
- 左画像パス
- 右画像パス
- 深度パス
- 幅と高さ
- 処理状態
- エラー内容

処理状態は、少なくとも次を持つ。

- `pending`
- `completed`
- `failed`

入力SVO2に関する以下の情報も、`config.yaml`または別のメタデータ領域へ記録する。

- 相対パス
- ファイルサイズ
- 更新日時
- 総フレーム数
- FPS
- 解像度
- カメラモデル
- ZED SDKバージョン

30GB前後のSVO2へ毎回全体ハッシュを計算すると時間がかかるため、既定の同一性確認には相対パス、ファイルサイズ、更新日時を用いる。厳密な確認が必要な場合のみ全体ハッシュを計算する。

## 12. 中断・再開

大容量SVO2を扱うため、中断後の再開を必須機能とする。

再開時には次の処理を行う。

1. 既存のmanifestを読み込む
2. `completed`の出力ファイルを検査する
3. 正常な項目はスキップする
4. `pending`または`failed`の項目だけを処理する
5. 成功・失敗状態をmanifestへ反映する

ファイルが存在するだけでは完了と判断せず、少なくとも以下を検査する。

- ファイルサイズが0ではない
- 画像を正常に読み込める
- 左、右、深度の解像度が一致する
- ファイル名とmanifestが対応する

ファイルは一時ファイルへ書き込み、正常に保存できた後で正式なファイル名へ置き換える。これにより、処理中断による不完全な出力を残しにくくする。

## 13. 既存出力の扱い

既定では既存出力を上書きしない。

```yaml
export:
  name: candidates_v1
  existing: error
```

対応する動作は次の通りとする。

| 値 | 動作 |
|---|---|
| `error` | 既存出力があれば停止する。安全な既定値 |
| `resume` | 既存manifestに従って未完了分のみ処理する |
| `skip` | 既存ファイルをスキップする |
| `overwrite` | 明示指定された場合のみ上書きする |

既存の`config.yaml`と今回の設定が異なる場合、`resume`や`skip`を許可せず、別の実行名を使用するように促す。

## 14. CLI案

単一のCLIに次のサブコマンドを用意する。

### 14.1 `inspect`

SVO2を探索し、総フレーム数、FPS、解像度、推定録画時間などを表示する。

```bash
python3 scripts/svo_extract.py inspect .
```

### 14.2 `plan`

画像を生成せず、対象フレームを決定してmanifestを作成する。

```bash
python3 scripts/svo_extract.py plan --config configs/extract.yaml
```

以下の概要も表示する。

- 対象SVO2数
- SVO2ごとの抽出予定枚数
- 合計抽出枚数
- 出力形式
- 深度設定
- 推定出力容量

### 14.3 `extract`

manifestに従って画像を抽出する。

```bash
python3 scripts/svo_extract.py extract --config configs/extract.yaml
```

再開する場合：

```bash
python3 scripts/svo_extract.py extract --config configs/extract.yaml --resume
```

### 14.4 `verify`

抽出済みファイルとmanifestの整合性を検査する。

```bash
python3 scripts/svo_extract.py verify \
  20260611-12Ehime/20260611_104824_exports/candidates_v1
```

## 15. 設定ファイル案

```yaml
input:
  root: .
  pattern: "*/*/recording.svo2"

export:
  name: candidates_v1
  existing: resume

sampling:
  method: stratified_random
  count_per_svo: 700
  seed: 42
  start_margin_seconds: 5
  end_margin_seconds: 5
  min_gap_seconds: 0

playback:
  strategy: hybrid
  seek_threshold_frames: 120

progress:
  interval_seconds: 5

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
    invalid_value: 0
    max_depth_meters: 40

  depth_preview:
    enabled: false
    format: png
    colormap: turbo

zed:
  depth_mode: NEURAL_LIGHT
  confidence_threshold: 50
  texture_confidence_threshold: 100

image:
  resolution: native
```

## 16. 初期実装の状況

2026年7月10日時点で、以下を実装済みである。

- 現在のディレクトリ構成からの`recording.svo2`探索
- `<セッションID>_exports/<実行名>/`への出力
- SVO2ごとの層化ランダム抽出
- 乱数シードによる再現
- 抽出前のmanifest作成
- 左右画像のJPEGおよびPNG出力
- 深度の16ビットPNGおよびNPY出力
- 同一画像IDによる左、右、深度の対応付け
- `config.yaml`への実行設定保存
- 中断・再開
- 対象フレームの昇順処理
- `seek`、`scan`および固定閾値の`hybrid`
- 抽出結果の検証
- 元データへ書き込まないことの保証

以下は、性能測定や利用上の必要性に基づいて追加を判断する。

- `hybrid`方式の自動閾値調整
- 画像書き込みの非同期化
- EXR深度出力
- 出力解像度変更
- SVO2単位の並列処理
- 厳密な入力ファイルハッシュ

## 17. 実装環境に関する現状

2026年7月10日の確認時点では、ローカル環境にZED SDK Python APIが存在し、SDKバージョンは5.1.0であった。GPUはNVIDIA GeForce RTX 5070 Ti、ドライバーバージョンは595.71.05、GPUメモリは16GBであることを権限外実行で確認した。

一方、以下の環境問題が確認されているため、実装・実行前に解消する必要がある。

- NumPy 2.3.4と、NumPy 1.x向けにビルドされたOpenCVのABI不整合により、`cv2`をimportできない
- サンドボックス内ではGPUデバイスへアクセスできないため、実SVO2の検証には権限外実行が必要

抽出プログラムはOpenCVに依存せず、ZED Python API、NumPy、Pillow、PyYAMLを使用する。専用のPython仮想環境を作成し、これらの互換バージョンを固定することが望ましい。

## 18. 参考資料

- [Stereolabs: Video Recording and SVO Playback](https://docs.stereolabs.com/docs/development/zed-sdk/modules/camera/recording)
- [Stereolabs: Using the Depth Sensing API](https://docs.stereolabs.com/docs/development/zed-sdk/modules/depth-sensing/using-the-api)
- [Stereolabs: Depth Sensing Tutorial](https://docs.stereolabs.com/docs/tutorials/depth-sensing)
- [Stereolabs: Depth Confidence Filtering](https://www.stereolabs.com/docs/depth-sensing/confidence-filtering)
