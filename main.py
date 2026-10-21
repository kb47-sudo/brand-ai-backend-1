import os
import io
import json
import re
import uuid
import time
import base64
import urllib.request
from PIL import Image, ImageEnhance
from fastapi import FastAPI, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional

app = FastAPI(title="Pro Precision Authenticator Engine", version="150.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

JOBS = {}

@app.get("/")
def read_root():
    return {
        "status": "Live",
        "engine": "Pro Precision Authenticator Engine v150.0 (48 Brands Master Rules Embedded)",
        "gemini_active": bool(GEMINI_API_KEY)
    }

def clean_old_jobs():
    now = time.time()
    for k in list(JOBS.keys()):
        if now - JOBS[k].get("created_at", now) > 900:
            JOBS.pop(k, None)

def get_available_gemini_models(api_key: str):
    """Google APIから現在利用可能なモデル一覧を直接取得して404エラーを防止"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=12) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                models = []
                for m in data.get("models", []):
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        name = m.get("name", "").replace("models/", "")
                        models.append(name)
                
                flash_models = [m for m in models if "flash" in m]
                other_models = [m for m in models if "flash" not in m]
                sorted_models = flash_models + other_models
                if sorted_models:
                    return sorted_models
    except Exception:
        pass
    return ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-pro"]

def call_gemini_direct(api_key: str, model_name: str, prompt: str, base64_images: list):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    parts = [{"text": prompt}]
    for b64 in base64_images:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": b64
            }
        })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json"
        }
    }

    try:
        json_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=json_bytes, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=35) as response:
            if response.status == 200:
                res_body = response.read().decode('utf-8')
                res_json = json.loads(res_body)
                if "candidates" in res_json and len(res_json["candidates"]) > 0:
                    cand = res_json["candidates"][0]
                    if "content" in cand and "parts" in cand["content"] and len(cand["content"]["parts"]) > 0:
                        return cand["content"]["parts"][0].get("text", "").strip(), None
                return None, "空の応答"
            else:
                return None, f"HTTP Status {response.status}"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            return None, f"HTTP {e.code}: {err_body[:100]}"
        except:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)

def execute_gemini_analysis(prompt: str, base64_images: list):
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEY が設定されていません。"

    target_models = get_available_gemini_models(GEMINI_API_KEY)
    logs = []

    for m_name in target_models:
        res_text, err = call_gemini_direct(GEMINI_API_KEY, m_name, prompt, base64_images)
        if res_text:
            return res_text, None
        logs.append(f"{m_name}: {err}")

    return None, f"Gemini通信詳細: {' | '.join(logs[:3])}"

# Step 1: モデル候補3選 検出処理
def process_candidate_job(job_id: str, brand: str, category: str, base64_images: list):
    prompt = f"""
あなたは日本の最高峰プロブランド鑑識士です。
送信された画像から対象アイテムの特定を行い、最も合致する【型番・モデル名候補】を3つ抽出してJSONで返してください。

ブランド: {brand}
カテゴリ: {category}

【出力JSONフォーマット】
{{
  "candidates": [
    {{"id": 1, "title": "[型番: M44336] ルイ・ヴィトン ネオノエ MM", "desc": "モノグラム・キャンバス"}},
    {{"id": 2, "title": "[型番: M40995] ルイ・ヴィトン ネヴァーフル MM", "desc": "モノグラム トートバッグ"}},
    {{"id": 3, "title": "[型番: M45254] ルイ・ヴィトン リポーター PM", "desc": "モノグラム ショルダー"}}
  ]
}}
"""
    response_text, err_msg = execute_gemini_analysis(prompt, base64_images)
    if response_text:
        try:
            cleaned_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL)
            cleaned_text = re.sub(r'```json\s*', '', cleaned_text)
            cleaned_text = re.sub(r'```\s*$', '', cleaned_text).strip()
            json_match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
            if json_match:
                result_data = json.loads(json_match.group(0))
                JOBS[job_id] = {"status": "completed", "data": result_data, "created_at": time.time()}
                return
        except Exception as e:
            err_msg = f"JSON解析エラー: {str(e)}"

    JOBS[job_id] = {
        "status": "completed",
        "created_at": time.time(),
        "data": {
            "candidates": [
                {"id": 1, "title": f"{brand} {category} (標準照合)", "desc": "画像から主要モデルを照合"},
                {"id": 2, "title": f"{brand} {category} (類似モデル)", "desc": "バリエーション型番"},
                {"id": 3, "title": f"{brand} {category} (手入力指定)", "desc": "型番を手入力で指定"}
            ]
        }
    }

# Step 2: 8部位精密鑑識＆公式定価・相場・真贋判定
def process_authentication_job(job_id: str, brand: str, category: str, model_number: str, base64_images: list):
    prompt = f"""
あなたは日本最高峰のブランド・ハイジュエリー・アパレル・高級時計の真贋鑑識士です。
送信された8部位の画像を以下の『全48ブランド480項目 マスター真贋基準』に従って1ミクロン単位で点検し、真贋結果・参考定価・4区分買取相場・違和感部位をJSONで出力してください。

対象ブランド: {brand}
カテゴリ: {category}
指定型番: {model_number}

【全48ブランド マスター真贋点検基準】
 1. ルイ・ヴィトン: ロゴ「O」正円、VUITTON「T」極小カーニング、ビス頭立体ドーム、ビス文字同心打刻、モノグラムシンメトリー、マスタードイエロー均一ステッチ、ジッパープルメッキ厚み、刻印深さ一定、シリアル規則/RFID IC応答。
 2. エルメス: 箔押し「É」アクサン分離、クウジュ・セリエ(左上→右下傾き手縫い)、コバ気泡・アバタなし、HERMÈS PARIS刻印バリなし、カデナ鍵穴刻印。
 3. シャネル: C断面斜めカット、A頂点フラット、E中央横線最短、右C上・左C下交差、金具裏マイナスネジ(プラスはNG)、シリアル0斜線/1足付き、365nm UV赤色発光、フラップキルティング格子線完全連続。
 4. セリーヌ: 現行「CELINE」(アクサンなし)/旧「CÉLINE」、型押し細身くっきり、トリオンフ金具艶、ステッチピッチ平行度。
 5. サンローラン: YSLロゴ上下交差重ね順、エッジバリなし、SAINT LAURENT PARIS「N」「T」接合、キルティング角度一致。
 6. プラダ: 三角プレート「R」右足V字くびれ、内ポケット底白タグ(1〜3桁数字)、ポコノナイロンハリ感、サフィアーノ格子均一性。
 7. グッチ: タグ®打刻丸み、G/C丸み、2段シリアル深さ、GGキャンバス左右非対称G＋2点配列、コントロールカード間隔。
 8. フェンディ: FFズッカ柄「F」上下横線差・隙間、バッグ左右縫い目FF左右対称、ピーカブー金具回転感度、セレリア手縫いステッチ。
 9. ボッテガ・ヴェネタ: イントレチャート裁断面(コバ)なめらかさ、内ポケット奥白タグ、革柔らかさ/匂い、マキシ編み込み精度。
10. ロエベ: アナグラム4つの「L」交差完璧さ、型押しエッジ、パズルバッグ切替コバ隙間なし、底鋲金属処理。
11. バレンシアガ: Boldロゴカーニング、内側タグ裏スタイルナンバー/カラーコード型押し、ヴィンテージ革シワ感。
12. クロエ: パドロック重量/メッキ質感、Woodyキャンバスリボン縫い目、フロントC金具厚み/エッジ削り出し。
13. ゴヤール: ヘリンボーン柄ドット鮮明度、ドット同士の未接触(滲み潰れは偽物)、サンルイハンドル根元縫製、麻/綿混合裏地。
14. ミュウミュウ: miu miuロゴ丸み/下部フラット、マテラッセギャザーふっくら感、ナッパレザー質感。
15. カルティエ: ホールマーク(18K鷲頭/Pt950犬頭)、筆記体Cartier細身刻印、LOVEビス凹み平滑面、7時/10時ローマ字隠し文字。
16. ティファニー: TIFFANY & CO. / Ag925 / AU750 / Pt950打刻、チェーンコマ完全溶接閉じ、オープンハート3次元曲線。
17. ハリー・ウィンストン: HWロゴ打刻平行、爪留め(プロング)極小研磨、プラチナ溶接痕完全無、メレダイヤF以上/VS以上。
18. ブルガリ: ダブルロゴ古代ローマ「V」深打刻、B.zero1バネ弾力、セルペンティ節々隙間均一。
19. ヴァン クリーフ＆アーペル: ペルレ金の粒完全球体、天然石と枠の隙間ゼロ、VCA 750 シリアル均一刻印、フランス鷲頭国印。
20. ショーメ: リアン・ドゥ・ショーメ重ね合わせ精度、ジョゼフィーヌ爪留め小ささ、アトラップ・モワ(蜂)切削痕。
21. ブシュロン: キャトル4層接合面隙間なし、クル・ド・パリピラミッドエッジ鋭利、グログラン畝等間隔。
22. ディオール: カナージュステッチ膨らみ、D.I.O.Rチャームバリなし、Diorオブリーク交差、金具マイナスネジ、RFID応答。
23. バーバリー: BURBERRY LONDON ENGLANDカーニング、ノバチェック縫い目左右対称、水牛角ボタン自然模様/彫刻。
24. メゾン マルジェラ: 背面白糸4本ステッチ長さ/角度、カレンダータグ○位置、Tabiつま先割れ目・補強、5AC LAMPOファスナー。
25. ドルチェ＆ガッバーナ: DOLCE&GABBANA「&」形状/スペースなし、金属プレート研磨精度、ボタン側面刻印。
26. ジバンシィ: 4Gロゴ左右対称性、GIVENCHY PARIS「G」「C」円形カーブ、アンティゴナ三角パッチ膨らみ。
27. ジョルジオ アルマーニ: 黒タグ刺繍密度、GAイーグル羽枚数/横線整列、スーツラペル八刺し立体感。
28. ヴァレンティノ: ロックスタッズピラミッド型エッジ鋭利さ/爪留め、Vロゴ金具厚み、VLTNプリント盛り上がり。
29. モンクレール: フェルトワッペンオンドリ刺繍密度、CHECKNOW QR照合、アニメコミックタグ角丸み、キャトルフロコンダウン復元力。
30. フェラガモ: オメガ型ガンチーニ金具肉厚感、ヴァラリボンプレート刻印深さ、靴底MADE IN ITALY打刻。
31. クリスチャン ルブタン: レッドソール赤ラッカー塗装艶/発色、ソール刻印深さ、スタッズ配置直列精度。
32. ロレックス: 6時位置サファイアガラス極小ドット王冠透かし、インナーリングルーレット刻印/6時シリアル深さ、文字盤油性立体盛り上げインク、サイクロップレンズ2.5倍拡大。
33. パテック フィリップ: カラトラバ十字エッジ鋭利、ムーブメント受けアングラージュ鏡面、PP/ジュネーブシール精密打刻。
34. オーデマ ピゲ: タペストリー機械彫り同心円、八角形ベゼル8本ビスフラット(ツライチ)、サテン/ポリッシュ鋭利エッジ。
35. ヴァシュロン・コンスタンタン: マルタ十字4端鋭利、オーバーシーズベゼル鏡面、22Kローター方位磁針彫刻。
36. リシャール・ミル: 5級チタン製トルクス型スプラインネジ山精度、NTPTカーボン層状模様/超軽量性、トノーケース湾曲。
37. オメガ: 12時Ωマーク左右対称、シーマスター裏蓋海馬レリーフ深さ、タキメーターベゼルDOT OVER 90印字。
38. ブレゲ: 手動旋盤ギヨシェ光拡散、ブレゲ針月形リング丸み/濃紺ブルースチール、ケース側面コインエッジ、シークレットサイン。
39. ジャガー・ルクルト: レベルソケーススライド反転スムーズさ、ケースホルダー内ペルラージュ研磨、1000時間テストメダル。
40. A.ランゲ＆ゾーネ: アウトサイズデイト金フレーム同一平面、ジャーマンシルバー3/4プレート、テンプ受け手彫り唐草模様/青焼きネジ。
41. IWC: IWC SCHAFFHAUSENフォント、風防カーブ合わせた針先曲げ、軟鉄製インナーケース耐磁重量。
42. ブライトリング: イカリ・翼Bロゴ立体感、ナビタイマー回転計算尺目盛り、パイロットブレス斜めコマ精密接合。
43. ウブロ: ベゼル/裏蓋6本H型ビス溝精度、異素材(ゴールド/セラミック/ラバー/カーボン)層状無隙間合体。
44. パネライ: ルミノールレバー式リューズガード噛み合わせ、サンドイッチ文字盤2層構造、OPロゴ矢印先端。
45. ゼニス: エル・プリメロ36000振動スイープ運針、3色インデックス文字盤、ゼニススターエッジ。
46. フランク ミュラー: ビザン数字インク立ち上がり、トノウ・カーベックス3次元曲線、裏蓋小径マイナスネジ。
47. ブランパン: フィフティファゾムスドーム型サファイアベゼル、ケース9時位置BLANCPAIN深刻印、18Kローター。
48. グランドセイコー: ザラツ研磨10倍ルーペ歪みゼロ完全鏡面、多面カット針/インデックス、スプリングドライブ完全スイープ運針。

【出力JSONフォーマット】
{{
  "detected_model": "{brand} {model_number} (マスター基準照合済)",
  "official_price": "272,800円 (メーカー参考定価)",
  "overall_score": 96.5,
  "judgment": "基準内",
  "status_code": "PASS",
  "message": "・真贋検査: マスター基準（ロゴ正円/カーニング/マイナスネジ/刻印深さ/縫製ピッチ/ホールマーク/ザラツ研磨面）を100%パスし「基準内」と判定いたしました。\\n・相場照合: 最新の二次流通市場相場をリアルタイム算出しました。",
  "suspicious_points": [],
  "market_prices": {{
    "buy_store": "130,000円 ～ 150,000円",
    "fleamarket": "160,000円 ～ 180,000円",
    "auction": "145,000円 ～ 165,000円",
    "full_set_estimate": "160,000円（美品完備時）"
  }},
  "details": [
    {{"name": "1. ブランドロゴフォント・正円/カーニング精度", "status": "PASS"}},
    {{"name": "2. シリアル/製造番号/ICチップ(RFID)規則性", "status": "PASS"}},
    {{"name": "3. ステッチピッチ・手縫い傾斜・縫製対称性", "status": "PASS"}},
    {{"name": "4. 金具・ビス・マイナスネジ構造・メッキ仕上げ", "status": "PASS"}},
    {{"name": "5. コバ塗り（バニッシュ）塗膜・気泡・はみ出し", "status": "PASS"}},
    {{"name": "6. 内装生地・ライニング・ファスナー(LAMPO/riri等)", "status": "PASS"}},
    {{"name": "7. ホールマーク・刻印底面・エッジ研磨(ザラツ面)", "status": "PASS"}},
    {{"name": "8. 全体コンディション・二次流通市場適合度", "status": "PASS"}}
  ]
}}
"""
    response_text, err_msg = execute_gemini_analysis(prompt, base64_images)

    if response_text:
        try:
            cleaned_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL)
            cleaned_text = re.sub(r'```json\s*', '', cleaned_text)
            cleaned_text = re.sub(r'```\s*$', '', cleaned_text).strip()

            json_match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
            if json_match:
                result_data = json.loads(json_match.group(0))
                JOBS[job_id] = {"status": "completed", "data": result_data, "created_at": time.time()}
                return
        except Exception as parse_e:
            err_msg = f"JSON解析エラー: {str(parse_e)}"

    JOBS[job_id] = {
        "status": "completed",
        "created_at": time.time(),
        "data": {
            "detected_model": f"{brand} {category} (解析エラー)",
            "official_price": "-- 円",
            "overall_score": 0.0,
            "judgment": "エラー発生",
            "status_code": "ERROR",
            "message": f"【解析失敗詳細】:\n{err_msg}",
            "suspicious_points": ["Gemini AI応答の取得に失敗しました"],
            "market_prices": {"buy_store": "-- 円", "fleamarket": "-- 円", "auction": "-- 円", "full_set_estimate": "-- 円"},
            "details": []
        }
    }

@app.post("/api/v1/detect_candidates")
async def detect_candidates(
    background_tasks: BackgroundTasks,
    brand: str = Form(...),
    category: str = Form(...),
    files: List[UploadFile] = File(...)
):
    if not files or len(files) < 1:
        raise HTTPException(status_code=400, detail="画像を1枚以上送信してください。")

    base64_images = []
    for file in files:
        contents = await file.read()
        try:
            image = Image.open(io.BytesIO(contents)).convert('RGB')
            image.thumbnail((600, 600))
            buffered = io.BytesIO()
            image.save(buffered, format="JPEG", quality=65)
            base64_images.append(base64.b64encode(buffered.getvalue()).decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"画像処理エラー: {str(e)}")

    clean_old_jobs()
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "processing", "created_at": time.time()}

    background_tasks.add_task(process_candidate_job, job_id, brand, category, base64_images)

    return {"status": "accepted", "job_id": job_id}

@app.post("/api/v1/authenticate")
async def authenticate(
    background_tasks: BackgroundTasks,
    brand: str = Form(...),
    category: str = Form(...),
    model_number: Optional[str] = Form(""),
    files: List[UploadFile] = File(...)
):
    if not files or len(files) < 1:
        raise HTTPException(status_code=400, detail="解析用画像を1枚以上送信してください。")

    base64_images = []
    for file in files:
        contents = await file.read()
        try:
            image = Image.open(io.BytesIO(contents)).convert('RGB')
            image.thumbnail((600, 600))
            image = ImageEnhance.Sharpness(image).enhance(1.2)

            buffered = io.BytesIO()
            image.save(buffered, format="JPEG", quality=65)
            base64_images.append(base64.b64encode(buffered.getvalue()).decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"画像処理エラー: {str(e)}")

    clean_old_jobs()
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "processing", "created_at": time.time()}

    background_tasks.add_task(process_authentication_job, job_id, brand, category, model_number, base64_images)

    return {"status": "accepted", "job_id": job_id}

@app.get("/api/v1/result/{job_id}")
def get_result(job_id: str):
    clean_old_jobs()
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="指定のジョブが見つかりません。")
    return JOBS[job_id]
