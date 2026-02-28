# import pymysql
# pymysql.install_as_MySQLdb()  # 让 peewee 以为有 MySQLdb/mysqlclient

import os
import re
import json
import jieba
from opencc import OpenCC
from dotenv import load_dotenv

if not os.getenv('GITHUB_ACTIONS'):
    load_dotenv(dotenv_path='.sora.env')
from peewee import *
from model.mysql_models import (
    DB_MYSQL, Product,Video, Document, SoraContent,  SoraMedia, FileTag, Tag, init_mysql
)
from database import ensure_connection
from model.scrap import Scrap
from utils.string_utils import LZString

SYNC_TO_POSTGRES = os.getenv('SYNC_TO_POSTGRES', 'false').lower() == 'true'
BATCH_LIMIT = None
# 初始化 MySQL（必须先执行）
init_mysql()

# 如需 PostgreSQL，再导入并初始化
if SYNC_TO_POSTGRES:
    from model.pg_models import DB_PG, SoraContentPg, SoraMediaPg, ProductPg, init_postgres
    from playhouse.shortcuts import model_to_dict
    init_postgres()
    # try:
    #     DB_PG.connect()
    #     print("✔️ DSN 方式下，connect() 成功！")
    #     cursor = DB_PG.execute_sql("SELECT 1;")
    #     print("[test query] SELECT 1 返回：", cursor.fetchone()[0])
    # except Exception as e:
    #     print("❌ DSN 方式下，connect() 失败：", e)
    # finally:
    #     if not DB_PG.is_closed():
    #         DB_PG.close()
    #         print("🔒 连接已关闭")

# 同义词字典
SYNONYM = {
    "滑鼠": "鼠标",
    "萤幕": "显示器",
    "笔电": "笔记本",
}

def clean_bj_text(original_string):
    target_strings = ["💾"]
    for target in target_strings:
        pos = original_string.find(target)
        if pos != -1:
            original_string = original_string[:pos]
    return original_string

def clean_text(original_string):
    target_strings = ["- Advertisement - No Guarantee", "- 广告 - 无担保"]
    for target in target_strings:
        pos = original_string.find(target)
        if pos != -1:
            original_string = original_string[:pos]

    replace_texts = [
        "求打赏", "求赏", "可通过以下方式获取或分享文件",
        "私聊模式：将含有File ID的文本直接发送给机器人 @datapanbot 即可进行文件解析",
        "①私聊模式：将含有File ID的文本直接发送给机器人  即可进行文件解析",
        "单机复制：", "文件解码器:", "您的文件码已生成，点击复制：",
        "批量发送的媒体代码如下:", "此条媒体分享link:",
        "女侅搜索：@ seefilebot", "解码：@ MediaBK2bot",
        "如果您只是想备份，发送 /settings 可以设置关闭此条回复消息",
        "媒体包已创建！", "此媒体代码为:", "文件名称:", "分享链接:", "|_SendToBeach_|",
        "Forbidden: bot was kicked from the supergroup chat",
        "Bad Request: chat_id is empty"
    ]
    for text in replace_texts:
        original_string = original_string.replace(text, '')

    original_string = re.sub(r"分享至\d{4}-\d{2}-\d{2} \d{2}:\d{2} 到期后您仍可重新分享", '', original_string)

    json_pattern = r'\{[^{}]*?"text"\s*:\s*"[^"]+"[^{}]*?\}'
    matches = re.findall(json_pattern, original_string)
    for match in matches:
        try:
            data = json.loads(match)
            if 'content' in data and isinstance(data['content'], str):
                original_string += f"\n{data['content']}"
        except json.JSONDecodeError:
            pass
        original_string = original_string.replace(match, '')

    wp_patterns = [r'https://t\.me/[^\s]+']
    for pattern in wp_patterns:
        original_string = re.sub(pattern, '', original_string)

    for pat in [
        r'LINK\s*\n[^\n]+#C\d+\s*\nOriginal:[^\n]*\n?',
        r'LINK\s*\n[^\n]+#C\d+\s*\nForwarded from:[^\n]*\n?',
        r'LINK\s*\n[^\n]*#C\d+\s*',
        r'Original caption:[^\n]*\n?'
    ]:
        original_string = re.sub(pat, '', original_string)

    original_string = re.sub(r'^\s*$', '', original_string, flags=re.MULTILINE)
    lines = original_string.split('\n')
    unique_lines = list(dict.fromkeys(lines))
    result_string = "\n".join(lines)

    for symbol in ['🔑', '💎']:
        result_string = result_string.replace(symbol, '\r\n' + symbol)

    return result_string[:1500] if len(result_string) > 1500 else result_string

def replace_synonym(text):
    for k, v in SYNONYM.items():
        text = text.replace(k, v)
    return text

def segment_text(text):
    text = replace_synonym(text)
    return " ".join(jieba.cut(text))

def fetch_tag_cn_for_file(file_unique_id):
    query = (
        Tag
        .select()
        .join(FileTag, on=(FileTag.tag == Tag.tag))
        .where(FileTag.file_unique_id == file_unique_id)
    )
    return [t.tag_cn for t in query if t.tag_cn]


def sync_to_postgres(record):
    if not SYNC_TO_POSTGRES:
        return

    from playhouse.shortcuts import model_to_dict

    IGNORED_FIELDS = {'content_seg_tsv', 'created_at', 'updated_at'}

    model_data = model_to_dict(record, recurse=False)
    model_data = {k: v for k, v in model_data.items() if k not in IGNORED_FIELDS}
    model_data["id"] = record.id  # 显式主键

    with DB_PG.atomic():
        try:
            existing = SoraContentPg.get(SoraContentPg.id == record.id)
            for k, v in model_data.items():
                setattr(existing, k, v)
            existing.save()
        except SoraContentPg.DoesNotExist:
            SoraContentPg.create(**model_data)

def sync_media_to_postgres(content_id, media_rows):
    if not SYNC_TO_POSTGRES:
        return

    with DB_PG.atomic():
        for media in media_rows:
            insert_data = {
                "content_id": content_id,
                "source_bot_name": media["source_bot_name"],
                "file_id": media["file_id"],
                "thumb_file_id": media["thumb_file_id"]
            }
            # print(f"Syncing media to PostgreSQL: {insert_data}")

            try:
                # print(f"🛰️ Syncing media to PostgreSQL: {insert_data}")

                SoraMediaPg.insert(**insert_data).on_conflict(
                    conflict_target=[SoraMediaPg.content_id, SoraMediaPg.source_bot_name],
                    update={k: insert_data[k] for k in ['file_id', 'thumb_file_id']}
                ).execute()

            except Exception as e:
                print(f"❌ 插入 PostgreSQL sora_media 失败: {e}")
                # print(f"   ➤ 失败内容: {insert_data}")

def process_documents():
    # DB_MYSQL.connect()
   


    print("\n🚀 开始同步 stage != 'updated' 的 document 到 PostgreSQL...",flush=True)
    with DB_MYSQL.atomic():
        for doc in Document.select().where((Document.kc_status.is_null(True)) | (Document.kc_status != 'updated')).limit(BATCH_LIMIT):
            if not doc.file_name and not doc.caption:
                doc.kc_status = 'updated'
                doc.save()
                continue

            jieba.load_userdict("jieba_userdict.txt")
            # 文本清洗与分词
            file_name = LZString.extract_meaningful_name(doc.file_name or '') or ''
            content = LZString.clean_text(f"{file_name}\n{doc.caption or ''}")
            content_seg = segment_text(content)

            # 标签分词追加
            tag_seg = ''
            tag_cn_list = fetch_tag_cn_for_file(doc.file_unique_id)
            if tag_cn_list:
                tag_seg = ' '.join(f'#{tag}' for tag in tag_cn_list)
                content_seg += " " + " ".join(tag_cn_list)


            # print(f"Processing {doc.file_unique_id}",flush=True)

            tw2s = OpenCC('tw2s')
            content_seg = tw2s.convert(content_seg)

            # 统一记录数据
            record_data = {
                'source_id': doc.file_unique_id,
                'file_type': 'd',
                'tag': tag_seg,
                'content': content,
                'content_seg': content_seg,
                'file_size': doc.file_size
            }

            # 使用 get_or_create 保证唯一性，避免 Duplicate
            kw, created = SoraContent.get_or_create(source_id=doc.file_unique_id, defaults=record_data)

            if not created:
                # 已存在，更新字段
                for key, value in record_data.items():
                    setattr(kw, key, value)
                kw.save()

            # 更新 Document 记录
            doc.kc_id = kw.id
            doc.kc_status = 'updated'
            doc.save()

            # 同步 PostgreSQL
            if SYNC_TO_POSTGRES and kw.id:
                sync_to_postgres(kw)

   



def process_videos():
   
    # DB_MYSQL.connect()


    print("\n🚀 开始同步 stage != 'updated' 的 video 到 PostgreSQL...")
    with DB_MYSQL.atomic():
        for doc in Video.select().where((Video.kc_status.is_null(True)) | (Video.kc_status != 'updated')).limit(BATCH_LIMIT):
            if not doc.file_name and not doc.caption:
                doc.kc_status = 'updated'
                doc.save()
                continue

            tag_seg = ''
            file_name = LZString.extract_meaningful_name(doc.file_name or '') or ''
            content = LZString.clean_text(f"{file_name or ''}\n{doc.caption or ''}")
            
            content_seg = segment_text(content)
            tag_cn_list = fetch_tag_cn_for_file(doc.file_unique_id)
            if tag_cn_list:
                tag_seg = ' '.join(f'#{tag}' for tag in tag_cn_list)
                content_seg += " " + " ".join(tag_cn_list)

            print(f"Processing {doc.file_unique_id}",flush=True)

            tw2s = OpenCC('tw2s')
            content_seg = tw2s.convert(content_seg)

            record_data = {
                'source_id': doc.file_unique_id,
                'file_type': 'v',
                'content': content,
                'tag': tag_seg,
                'content_seg': content_seg,
                'file_size': doc.file_size,
                'duration': doc.duration
            }

            kw, created = SoraContent.get_or_create(source_id=doc.file_unique_id, defaults=record_data)

            if not created:
                for key, value in record_data.items():
                    setattr(kw, key, value)
                kw.save()

            # print(f"  🔄 更新 MySQL sora_content [{kw}]",flush=True)

            # print(kw.__data__)

            doc.kc_id = kw.id
            doc.kc_status = 'updated'
            doc.save()

            if SYNC_TO_POSTGRES and kw.id:
                sync_to_postgres(kw)

  



def parse_bj_tag_for_file(tag_str):
    tag_cn_list = []
    if tag_str:
       # 将 tag_str 按空白符号分割
        tag_list = tag_str.split()
        for tag in tag_list:
            # 移除 tag 前的 #
            tag = tag.lstrip('#')
            
            tag_mapping = {
                "白种人": "白人",
                "黑种人": "黑人",
                "露脸": "有露脸",
                "遮挡": "带了面罩",
                "未露脸": "没有露脸",
                "无毛": "高年级_小五",
                "中毛": "少年_高中",
                "黑森林": "少年_高中",
                "幼儿": "低年级_小二",
                "小学": "低年级_小二",
                "初中": "初毛",
                "高中": "少年_高中",
                "合法": "少年_高中",
                "清水": "没有裸体",
                "写真": "正太主题汇整",
                "动画": "卡通动漫",
                "母子": "正太与阿姨",
                "肛交": "爆菊",
                "口交": "口交",
                "足交": "恋足",
                "自撸": "撸管",
                "射精": "射精",
                "展示": "正太独秀",
                "摸": "手交",
                "偷拍": "偷拍",
                "胖太": "胖太",
                "TK挠痒": "瘙痒",
                "SP打屁股": "打屁股",
                "恋物": "恋物",
                "猎奇重口": "猎奇",
                "医学类": "医学",
                "霸凌": "霸凌",
                "监视": "监视器",
                "直播录屏": "直播",
                "冰淇淋": "冰淇淋",
                "奶黄包": "奶黄包",
                "眼镜哥": "眼镜哥系列",
                "西边的风":"西边的风",
                "日本全方位":"日本全方位",
                "小孩不笨":"小孩不笨",  #
                "网调大神":"网调大神",  #
                "猫系列":"猫系列",  
                "雨花石":"雨花石系列",  
                "早点睡觉caodidi":"Caodidi系列",
                "BB嵬":"BB嵬",
                "we出品":"WE系列",
                "堂山天草":"堂山",
                "么么哒视频":"么么哒视频", #
                "么么哒举牌原创":"么么哒举牌原创", #
                "小6童模":"小六摄影",
                "ledi二维码":"二维码系列"
            }

            # 替换标签
            tag = tag_mapping.get(tag, tag)  # 如果 tag 不在映射中，就保留原值
            tag_cn_list.append(tag)

    return tag_cn_list

def process_scrap():
    # DB_MYSQL.connect()
    


    print("\n🚀 开始同步 stage != 'updated' 的 scrap 到 PostgreSQL...")
    for scrap in Scrap.select().where(((Scrap.kc_status.is_null(True)) | (Scrap.kc_status != 'updated')) & (Scrap.thumb_file_unique_id != '')).limit(BATCH_LIMIT):
        if not scrap.content:
            scrap.kc_status = 'updated'
            scrap.save()
            continue

        content = clean_bj_text(scrap.content or '')
        content = LZString.clean_text(content)
        content_seg = segment_text(content)

        tag_seg = ''
        if scrap.tag:
            tag_cn_list = parse_bj_tag_for_file(scrap.tag)
            tag_seg = ' '.join(f'#{tag}' for tag in tag_cn_list)
            content_seg += " " + " ".join(tag_cn_list)

        # print(f"Processing {scrap.id}: {content_seg}")

        tw2s = OpenCC('tw2s')
        content_seg = tw2s.convert(content_seg)

        record_data = {
            'source_id': scrap.id,
            'file_type': 's',
            'content': content,
            'content_seg': content_seg,
            'tag': tag_seg,
            'file_size': scrap.estimated_file_size,
            'duration': scrap.duration,
            'thumb_file_unique_id': scrap.thumb_file_unique_id,
            'thumb_hash': scrap.thumb_hash
        }

        kw, created = SoraContent.get_or_create(source_id=scrap.id, defaults=record_data)

        if not created:
            for key, value in record_data.items():
                setattr(kw, key, value)
            kw.save()

        scrap.kc_id = kw.id
        scrap.kc_status = 'updated'
        scrap.save()

        media_data = [{
            'source_bot_name': scrap.thumb_bot,
            'file_id': None,
            'thumb_file_id': scrap.thumb_file_id
        }]

        for media in media_data:
            existing = SoraMedia.select().where(
                (SoraMedia.content_id == scrap.kc_id) &
                (SoraMedia.source_bot_name == media["source_bot_name"])
            ).first()

            if existing:
                existing.file_id = media["file_id"]
                existing.thumb_file_id = media["thumb_file_id"]
                existing.save()
                print(f"  🔄 更新 MySQL sora_media [{media['source_bot_name']}]")
            else:
                SoraMedia.create(content_id=scrap.kc_id, **media)
                print(f"  ✅ 新增 MySQL sora_media [{media['source_bot_name']}]")

        if SYNC_TO_POSTGRES and kw.id:
            sync_to_postgres(kw)
            sync_media_to_postgres(scrap.kc_id, media_data)
            print("🚀 同步到 PostgreSQL 完成")

  


def process_sora_update():
    import time
   
    # DB_MYSQL.connect()


    sora_content_rows = SoraContent.select().where(SoraContent.stage=="pending").limit(BATCH_LIMIT)
    print(f"📦 正在处理 {len(sora_content_rows)} 笔 sora 数据...\n",flush=True)

    for row in sora_content_rows:
        source_id = row.source_id
        print(f"🔍 处理 source_id: {source_id}",flush=True)

        content = {
            'source_id': source_id,
            'content': row.content or '',
            'owner_user_id': row.user_id,
            'source_channel_message_id': row.source_channel_message_id,
            'thumb_file_unique_id': row.thumb_file_unique_id,
            'thumb_hash': row.thumb_hash,
            'file_size': row.file_size,
            'duration': row.duration,
            'tag': row.tag,
            'file_type': row.file_type[0] if row.file_type else None,
            'valid_state': row.valid_state,
            'plan_update_timestamp': row.plan_update_timestamp,
            'stage': row.stage
        }

        # 插入或更新 SoraContent
        sora_content, created = SoraContent.get_or_create(source_id=source_id, defaults=content)
        if created:
            print("✅ 新增 MySQL sora_content",flush=True)
        else:
            for k, v in content.items():
                setattr(sora_content, k, v)
            sora_content.save()
            print("🔄 更新 MySQL sora_content",flush=True)

        # 建立 SoraMedia（两个机器人来源）
        media_data = [
            {
                'source_bot_name': row.source_bot_name,
                'file_id': row.file_id,
                'thumb_file_id': row.thumb_file_id
            },
            {
                'source_bot_name': row.shell_bot_name,
                'file_id': row.shell_file_id,
                'thumb_file_id': row.shell_thumb_file_id
            }
        ]

        for media in media_data:
            existing = SoraMedia.select().where(
                (SoraMedia.content_id == sora_content.id) &
                (SoraMedia.source_bot_name == media["source_bot_name"])
            ).first()

            if existing:
                existing.file_id = media["file_id"]
                existing.thumb_file_id = media["thumb_file_id"]
                existing.save()
                print(f"  🔄 更新 MySQL sora_media [{media['source_bot_name']}]",flush=True)
            else:
                SoraMedia.create(content_id=sora_content.id, **media)
                print(f"  ✅ 新增 MySQL sora_media [{media['source_bot_name']}]",flush=True)


        # 更新原始表状态
        row.update_content = int(time.time())
        row.save()

        # 同步到 PostgreSQL
        if SYNC_TO_POSTGRES:
            sync_to_postgres(sora_content)
            sync_media_to_postgres(sora_content.id, media_data)
            print("🚀 同步到 PostgreSQL 完成",flush=True)

   


def sync_pending_sora_to_postgres():
    if not SYNC_TO_POSTGRES:
        print("🔒 SYNC_TO_POSTGRES 为 False，跳过 PostgreSQL 同步", flush=True)
        return

    print("\n🚀 开始同步 stage = 'pending' 的 sora_content 到 PostgreSQL...", flush=True)
    from playhouse.shortcuts import model_to_dict

   

    # ✅ 关键：复用已开启连接；如果外部已 connect，这里不会报错
    DB_PG.connect(reuse_if_open=True)

    rows = SoraContent.select().where(SoraContent.stage == "pending").limit(BATCH_LIMIT)

    for row in rows:
        model_data = model_to_dict(row, recurse=False)

        for ignored in ('content_seg_tsv', 'created_at', 'updated_at'):
            model_data.pop(ignored, None)

        model_data["id"] = row.id  # MySQL/PG 主键一致

        with DB_PG.atomic():
            try:
                existing = SoraContentPg.get(SoraContentPg.id == row.id)
                for k, v in model_data.items():
                    setattr(existing, k, v)
                existing.save()
                print(f"✅ 已更新 PostgreSQL sora_content.id = {row.id}", flush=True)
            except SoraContentPg.DoesNotExist:
                SoraContentPg.create(**model_data)
                print(f"✅ 已新增 PostgreSQL sora_content.id = {row.id}", flush=True)

        # 回写 MySQL：stage = updated
        row.stage = "updated"
        row.save()

    # ❗不要在这里 close DB_PG（交给 __main__ finally 统一关）



def sync_pending_sora_to_postgres2():
    if not SYNC_TO_POSTGRES:
        print("🔒 SYNC_TO_POSTGRES 为 False，跳过 PostgreSQL 同步",flush=True)
        return

    print("\n🚀 开始同步 stage = 'pending' 的 sora_content 到 PostgreSQL...",flush=True)
    from playhouse.shortcuts import model_to_dict

    # DB_MYSQL.connect()
    
    DB_PG.connect()

    rows = SoraContent.select().where(SoraContent.stage == "pending").limit(BATCH_LIMIT)

    for row in rows:
        # print(f"🔄 同步中：source_id = {row.source_id}",flush=True)

        model_data = model_to_dict(row, recurse=False)
        # 去除不必要字段
        for ignored in ('content_seg_tsv', 'created_at', 'updated_at'):
            model_data.pop(ignored, None)
        model_data["id"] = row.id  # 强制使用相同主键

        try:
            existing = SoraContentPg.get(SoraContentPg.id == row.id)
            for k, v in model_data.items():
                setattr(existing, k, v)
            existing.save()
            print(f"✅ 已更新 PostgreSQL sora_content.id = {row.id}",flush=True)
        except SoraContentPg.DoesNotExist:
            SoraContentPg.create(**model_data)
            print(f"✅ 已新增 PostgreSQL sora_content.id = {row.id}",flush=True)

        # ✅ 回写 MySQL：stage = "updated"
        row.stage = "updated"
        row.save()
        # print(f"📝 已更新：source_id{row.source_id} =>MySQL sora_content.stage = 'updated'",flush=True)


   
    DB_PG.close()

def sync_pending_product_to_postgres_old():
    from peewee import IntegrityError
    if not SYNC_TO_POSTGRES:
        print("🔒 SYNC_TO_POSTGRES 为 False，跳过 PostgreSQL 同步",flush=True)
        return

    print("\n🚀 开始同步 stage = 'pending' 的 product 到 PostgreSQL...",flush=True)
    from playhouse.shortcuts import model_to_dict

    # DB_MYSQL.connect()
   
    DB_PG.connect()

    rows = Product.select().where(Product.stage == "pending").limit(BATCH_LIMIT)

    for row in rows:
        # print(f"🔄 同步中：source_id = {row.source_id}")

        model_data = model_to_dict(row, recurse=False)
        # 去除不必要字段
        for ignored in ('stage',):
            model_data.pop(ignored, None)

        model_data["content_id"] = row.content_id  # 强制使用相同主键

        from peewee import IntegrityError

        try:
            existing = ProductPg.get(ProductPg.content_id == row.content_id)
            for k, v in model_data.items():
                setattr(existing, k, v)
            existing.save()

        except ProductPg.DoesNotExist:
            try:
                ProductPg.create(**model_data)

            except IntegrityError as e:
                msg = str(e)
                # 只处理你指定的：product_pkey 主键冲突
                if 'duplicate key value violates unique constraint "product_pkey"' in msg:
                    conflict_id = model_data.get("id")
                    if conflict_id is None:
                        raise  # 没有 id 无法执行“删再插”，交回上层处理

                    print(f"⚠️ product_pkey 冲突：id={conflict_id}，强制删除 PostgreSQL 旧记录后重建", flush=True)

                    # 强制删除冲突主键行
                    ProductPg.delete().where(ProductPg.id == conflict_id).execute()

                    # 再插入一次
                    ProductPg.create(**model_data)

                else:
                    # 不是 product_pkey 的冲突，不做破坏性操作
                    raise


        # ✅ 回写 MySQL：stage = "updated"
        row.stage = "updated"
        row.save()
        # print(f"📝 已更新：content_id{row.content_id} =>MySQL Product.stage = 'updated'",flush=True)


 
    DB_PG.close()


from peewee import IntegrityError

def sync_pending_product_to_postgres():
    if not SYNC_TO_POSTGRES:
        print("🔒 SYNC_TO_POSTGRES 为 False，跳过 PostgreSQL 同步", flush=True)
        return

    print("\n🚀 开始同步 stage = 'pending' 的 product 到 PostgreSQL...", flush=True)
    from playhouse.shortcuts import model_to_dict

    
    DB_PG.connect(reuse_if_open=True)

    rows = Product.select().where(Product.stage == "pending").limit(BATCH_LIMIT)

    def _is_unique_violation(e: Exception, constraint: str) -> bool:
        s = str(e)
        return ('duplicate key value violates unique constraint' in s) and (f'"{constraint}"' in s)

    def _upsert_by_id_once(model_data: dict, target_id: int):
        # 关键：让 IntegrityError 直接抛出 atomic()，触发自动 rollback
        with DB_PG.atomic():
            existing = ProductPg.get_or_none(ProductPg.id == target_id)
            if existing is None:
                ProductPg.create(**model_data)
            else:
                for k, v in model_data.items():
                    setattr(existing, k, v)
                existing.save()

    def _delete_conflict_content_id(target_content_id: int, target_id: int):
        # 独立事务做清理
        with DB_PG.atomic():
            (ProductPg
             .delete()
             .where((ProductPg.content_id == target_content_id) & (ProductPg.id != target_id))
             .execute())

    def _delete_conflict_id(target_id: int):
        with DB_PG.atomic():
            ProductPg.delete().where(ProductPg.id == target_id).execute()

    for row in rows:
        model_data = model_to_dict(row, recurse=False)
        model_data.pop("stage", None)

        target_id = model_data.get("id")
        target_content_id = model_data.get("content_id")

        if target_id is None:
            raise ValueError("Product row has no id; cannot sync by id")

        try:
            # 1) id 优先：先 upsert
            _upsert_by_id_once(model_data, target_id)

        except IntegrityError as e:
            # 2) content_id 冲突：删掉占用该 content_id 的其他行，再重试
            if _is_unique_violation(e, "uq_product_content_id"):
                print(
                    f"⚠️ uq_product_content_id 冲突：content_id={target_content_id}，删除重复 content_id 后重试",
                    flush=True
                )
                _delete_conflict_content_id(target_content_id, target_id)
                _upsert_by_id_once(model_data, target_id)

            # 3) 主键冲突：删同 id 的旧行，再重试；若仍撞 content_id，再删 content_id 占用者再试一次
            elif _is_unique_violation(e, "product_pkey"):
                print(f"⚠️ product_pkey 冲突：id={target_id}，删除同 id 后重试", flush=True)
                _delete_conflict_id(target_id)

                try:
                    _upsert_by_id_once(model_data, target_id)
                except IntegrityError as e2:
                    if _is_unique_violation(e2, "uq_product_content_id"):
                        print(
                            f"⚠️ uq_product_content_id 冲突（在删除 id 后仍发生）：content_id={target_content_id}，删除重复 content_id 后再重试",
                            flush=True
                        )
                        _delete_conflict_content_id(target_content_id, target_id)
                        _upsert_by_id_once(model_data, target_id)
                    else:
                        raise
            else:
                raise

        # ✅ 回写 MySQL：stage = "updated"
        row.stage = "updated"
        row.save()

        print(f"✅ 已同步 PostgreSQL product.id={target_id}, content_id={target_content_id}", flush=True)

    # 不要在这里 close PG（建议统一在 __main__ finally 关）
    # DB_MYSQL.close() / DB_PG.close() 交给主流程统一管理



def sync_pending_product_to_postgres2():
    from peewee import IntegrityError

    if not SYNC_TO_POSTGRES:
        print("🔒 SYNC_TO_POSTGRES 为 False，跳过 PostgreSQL 同步", flush=True)
        return

    print("\n🚀 开始同步 stage = 'pending' 的 product 到 PostgreSQL...", flush=True)
    from playhouse.shortcuts import model_to_dict

 
    DB_PG.connect()

    rows = Product.select().where(Product.stage == "pending").limit(BATCH_LIMIT)

    def _is_unique_violation(e: Exception, constraint: str) -> bool:
        s = str(e)
        return ('duplicate key value violates unique constraint' in s) and (f'"{constraint}"' in s)

    for row in rows:
        model_data = model_to_dict(row, recurse=False)
        model_data.pop("stage", None)

        target_id = model_data.get("id")
        target_content_id = model_data.get("content_id")

        if target_id is None:
            raise ValueError("Product row has no id; cannot sync by id")

        with DB_PG.atomic():
            # 1) 以 id 为主：先找同 id 的 existing
            existing = ProductPg.get_or_none(ProductPg.id == target_id)

            try:
                if existing is None:
                    # INSERT
                    ProductPg.create(**model_data)
                else:
                    # UPDATE（按 id 更新这一条）
                    for k, v in model_data.items():
                        setattr(existing, k, v)
                    existing.save()

            except IntegrityError as e:
                # 2) 若 content_id 唯一冲突：删除“占用该 content_id 的其他行”，再重试一次
                if _is_unique_violation(e, "uq_product_content_id"):
                    print(
                        f"⚠️ uq_product_content_id 冲突：content_id={target_content_id}，删除重复 content_id 后重试",
                        flush=True
                    )

                    # 只删除“同 content_id 且 id != 当前 id”的行，避免误删当前目标
                    (ProductPg
                     .delete()
                     .where((ProductPg.content_id == target_content_id) & (ProductPg.id != target_id))
                     .execute())

                    # 重试（同一事务内）
                    existing2 = ProductPg.get_or_none(ProductPg.id == target_id)
                    if existing2 is None:
                        ProductPg.create(**model_data)
                    else:
                        for k, v in model_data.items():
                            setattr(existing2, k, v)
                        existing2.save()

                # 3) 若主键冲突（极少数：并发/历史异常）：删同 id 再插一次
                elif _is_unique_violation(e, "product_pkey"):
                    print(
                        f"⚠️ product_pkey 冲突：id={target_id}，删除同 id 后重试",
                        flush=True
                    )
                    ProductPg.delete().where(ProductPg.id == target_id).execute()

                    # 这里再次插入仍可能因 content_id 冲突失败，让上层看到_


if __name__ == "__main__":
    ensure_connection()

    if SYNC_TO_POSTGRES:
        # 只连一次
        DB_PG.connect(reuse_if_open=True)

    try:
        process_documents()            # 里面不要再 connect/close PG
        process_videos()               # 同上
        # process_scrap()
        sync_pending_sora_to_postgres()  # 同上
        sync_pending_product_to_postgres()
    finally:
        # 统一关闭
        if not DB_MYSQL.is_closed():
            DB_MYSQL.close()
        if SYNC_TO_POSTGRES and (not DB_PG.is_closed()):
            DB_PG.close()

