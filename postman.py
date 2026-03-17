#!/usr/bin/env python
# pylint: disable=unused-argument

import asyncio
import time
import os

# 加载环境变量
if not os.getenv('GITHUB_ACTIONS'):
    from dotenv import load_dotenv
    load_dotenv(dotenv_path='.20100034.sungfong.env')
    # load_dotenv(dotenv_path='.20100034.luzai09man.env')
    # load_dotenv(dotenv_path='.x.env')
    # load_dotenv(dotenv_path='.28817994.get_account.env')
    # load_dotenv(dotenv_path='.28817994.luzai.env')
    # load_dotenv(dotenv_path='.25254811.bjd.env', override=True)
    # load_dotenv(dotenv_path='.25299903.warehouse.env', override=True)
    



import random
import re
import json
from datetime import datetime
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage
from telethon.tl.types import InputMessagesFilterEmpty
from telethon.tl.types import PeerChannel

import pymysql
pymysql.install_as_MySQLdb()  # 让 peewee 等库以为它就是 MySQLdb

from peewee import DoesNotExist

from model.scrap_progress import ScrapProgress
from model.scrap_config import ScrapConfig
from database import db

from handlers.HandlerBJIClass import HandlerBJIClass
from handlers.HandlerBJILiteClass import HandlerBJILiteClass
from handlers.HandlerNoAction import HandlerNoAction
from handlers.HandlerNoDelete import HandlernNoDeleteClass

from handlers.HandlerRelayClass import HandlerRelayClass
from handlers.HandlerPrivateMessageClass import HandlerPrivateMessageClass

from telethon import functions, types
from telethon.errors import RPCError, ChannelPrivateError, FloodWaitError
from telethon.tl.functions.photos import DeletePhotosRequest
from telethon.tl.types import InputPhoto
from telethon.tl.types import ChannelForbidden
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.account import UpdateUsernameRequest
from telethon.tl.functions.channels import InviteToChannelRequest, TogglePreHistoryHiddenRequest
from telethon.tl.types import PeerUser
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest


from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import Message

SOURCE_CHAT_ID = 777000               # Telegram 服务讯息
TARGET_USER_ID = 7550420493           # 接收者 user_id（整数）
# OLD_PASSWORD = "008009"
# OLD_PASSWORD = "258147"
OLD_PASSWORD = "Qqqw1234"
# NEW_PASSWORD = "Qqqw1234"
NEW_PASSWORD = "008009"
HINT         = "myhint"                  # 可选：密码提示


# 只保留这些 session 的 hash
WHITELIST = {
    "Redmi Redmi K40",                       # PC 64bit Android
    "XiaomiM2012K11AC",     # XiaomiM2012K11AC
    "PC 64bit",     # PC 64bit
}

# 配置参数
config = {
    'api_id': os.getenv('API_ID',''),
    'api_hash': os.getenv('API_HASH',''),
    'phone_number': os.getenv('PHONE_NUMBER',''),
    'setting_chat_id': int(os.getenv('SETTING_CHAT_ID') or 0),
    'setting_thread_id': int(os.getenv('SETTING_THREAD_ID') or 0),
    'setting' : os.getenv('CONFIGURATION', '')
}

SESSION_STRING  = os.getenv("USER_SESSION_STRING")

# print(f"⚠️ 配置參數：{config}", flush=True)




# 嘗試載入 JSON 並合併參數
try:
    setting_json = json.loads(config['setting'])
   
    if isinstance(setting_json, dict):
        config.update(setting_json)  # 將 JSON 鍵值對合併到 config 中
except Exception as e:
    print(f"⚠️ 無法解析 CONFIGURATION：{e}")

# print(f"⚠️ 配置參數：{config}", flush=True)

config['session_name'] = str(config['api_id']) + 'session_name'  # 确保 session_name 正确

'''
协义号
'''
# SESSION_STRING=None
# config['session_name'] = "916303192358"
# config['phone_number'] = "+916303192358"


# print(f"⚠️ 配置參數：{config}")
   
# 在模块顶部初始化全局缓存
local_scrap_progress = {}  # key = (chat_id, api_id), value = message_id



# 黑名单缓存
blacklist_entity_ids = set()

# 初始化 Telegram 客户端


if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), config['api_id'], config['api_hash'])
    print("【Telethon】使用 StringSession 登录。",flush=True)
else:
    client = TelegramClient(config['session_name'], config['api_id'], config['api_hash'])
    print("【Telethon】使用普通会话登录。",flush=True)


# 常量
MAX_PROCESS_TIME = 5 * 60  # 最大运行时间 5 分钟

# Class Map
raw_class_map = config.get("class_map", {})
class_map = {}
for chat_id_str, entry in raw_class_map.items():
    try:
        chat_id = int(chat_id_str)
        handler_class_name = entry.get("handler")

        # ✅ 使用 globals() 自动取出提前 import 的类
        handler_class = globals().get(handler_class_name)

        if handler_class:
            class_map[chat_id] = {
                "handler_class": handler_class,
                "save_progress": entry.get("save_progress", True)
            }
        else:
            print(f"⚠️ 未识别的 handler 类名: {handler_class_name}")
    except Exception as e:
        print(f"⚠️ 解析 class_map[{chat_id_str}] 失败: {e}")

current_user_name = ''
max_message_id = 0

async def join(invite_hash):
    from telethon.tl.functions.messages import ImportChatInviteRequest
    try:
        await client(ImportChatInviteRequest(invite_hash))
        print("已成功加入群组")
    except Exception as e:
        if 'InviteRequestSentError' in str(e):
            print("加入请求已发送，等待审批")
        else:
            print(f"失败-加入群组: {invite_hash} {e}")

async def safe_remove_forbidden(entity):
    # 用一个“假”的 InputPeerChannel，只要有 channel_id 就够了
    fake_peer = types.InputPeerChannel(entity.id, 0)
    try:
        # 直接调用底层的 messages.DeleteDialogRequest，
        # 它只会把对话从列表里删掉，不会退群。
        await client(functions.messages.DeleteDialogRequest(peer=fake_peer))
        print(f"✅ 本地删除对话（不会退群）：{entity.id}")
    except RPCError as e:
        print(f"⚠️ DeleteDialogRequest 失败：{e}")

async def leave_group(entity):
    from telethon.tl.types import InputPeerChannel

    try:
        fake_peer = InputPeerChannel(channel_id=entity.id, access_hash=0)
        await client.delete_dialog(fake_peer, revoke=True)
        print(f'✅ 已安全退出/删除频道: {getattr(entity, "title", entity.id)}')
    except Exception as e:
        print(f'❌ 删除失败: {e}')

async def open_chat_history(entity):
    try:
        result = await client(TogglePreHistoryHiddenRequest(
            channel=entity,
            enabled=False  # False = 允许新成员查看历史记录
        ))
        print(f'✅ 已开启历史记录可见: {result}')
    except Exception as e:
        print(f'❌ 操作失败: {e}')

async def delete_my_profile_photos(client):
    photos = await client.get_profile_photos('me')

    if not photos:
        print("你没有设置头像。")
        return

    input_photos = []
    for photo in photos:
        if hasattr(photo, 'id') and hasattr(photo, 'access_hash') and hasattr(photo, 'file_reference'):
            input_photos.append(InputPhoto(
                id=photo.id,
                access_hash=photo.access_hash,
                file_reference=photo.file_reference
            ))

    await client(DeletePhotosRequest(id=input_photos))
    print("头像已删除。")

async def update_my_name(client, first_name, last_name=''):
    await client(UpdateProfileRequest(first_name=first_name, last_name=last_name))
    print(f"已更新用户姓名为：{first_name} {last_name}")

async def update_username(client,username):
    try:
        await client(UpdateUsernameRequest(username))  # 设置空字符串即为移除
        print("用户名已成功变更。")
    except Exception as e:
        print(f"变更失败：{e}")

async def invite_bot(bot_username, entity):
# 获取 Bot 实体
    bot_entity = await client.get_entity(bot_username)
    # 邀请 Bot 到超级群
    try:
        await client.send_message(bot_username, '/start')
        await client.send_message(bot_username, 'Hello')
        await client(InviteToChannelRequest(
            channel=entity,
            users=[bot_entity]
        ))
        print(f'已邀请 @{bot_username} 进入本群')

        # 检查是否真的在群里
        participants = await client.get_participants(entity)
        if any(p.username and p.username.lower() == bot_username.lower() for p in participants):
            print(f'✅ 确认 @{bot_username} 已经加入')
        else:
            print(f'⚠️ @{bot_username} 似乎没有加入，可能已被踢出或受限')

    except Exception as e:
        print(f'邀请失败: {e}')

async def safe_delete_message(message):
    try:
        await client.delete_messages(message.chat_id, [message.id], revoke=True)
        print(f"🧹 成功刪除訊息A {message.id}（雙方）", flush=True)
    except Exception as e:
        print(f"⚠️ 刪除訊息失敗A {message.id}：{e}", flush=True)

async def add_contact():

    # 构造一个要导入的联系人
    contact = InputPhoneContact(
        client_id=0, 
        phone="+18023051359", 
        first_name="DrXP", 
        last_name=""
    )

    result = await client(ImportContactsRequest([contact]))
    print("导入结果:", result)
    target = await client.get_entity(TARGET_USER_ID)     # 7550420493


    me = await client.get_me()
    await client.send_message(target, f"你好, 我是 {me.id} - {me.first_name} {me.last_name or ''}")


async def keep_db_alive():
    if db.is_closed():
        db.connect()
    else:
        try:
            db.execute_sql('SELECT 1')
        except Exception as e:
            print(f"数据库连接保持错误: {e}")

async def send_completion_message(last_message_id):
    try:
        print(f"发送完成消息到 {config['setting_chat_id']} 线程 {config['setting_thread_id']}")
        if config['setting_chat_id'] == 0 or config['setting_thread_id'] == 0:
            print("未设置配置线程 ID，无法发送完成消息。")
            return
        async with client.conversation(config['setting_chat_id']) as conv:
            await conv.send_message('ok', reply_to=config['setting_thread_id'])
    except Exception as e:
        print("未设置配置线程 ID，无法发送完成消息。")
        pass

async def is_blacklisted(entity_id):
    global blacklist_entity_ids

    # ✅ 先查缓存
    if entity_id in blacklist_entity_ids:
        return True

    # ✅ 先尝试从 ScrapConfig 取黑名单
    try:
        record = ScrapConfig.get(
            (ScrapConfig.api_id == config['api_id']) &
            (ScrapConfig.title == 'BLACKLIST_IDS')
        )
        raw = record.value or ''
        
        ids = {int(x.strip()) for x in raw.split(',') if x.strip().isdigit()}
        blacklist_entity_ids.update(ids)  # 缓存

        return entity_id in blacklist_entity_ids
    except DoesNotExist:
        blacklist_entity_ids = set()
        # print("⚠️ scrap_config 中找不到 BLACKLIST_IDS")
        return False
    except Exception as e:
        print(f"⚠️ 加载黑名单失败: {e}")
        return False

async def get_max_source_message_id(source_chat_id):
    key = (source_chat_id, config['api_id'])
    if key in local_scrap_progress:
        return local_scrap_progress[key]

    try:
        record = ScrapProgress.select().where(
            (ScrapProgress.chat_id == source_chat_id) &
            (ScrapProgress.api_id == config['api_id'])
        ).order_by(ScrapProgress.update_datetime.desc()).limit(1).get()

        local_scrap_progress[key] = record.message_id
        return record.message_id

    except DoesNotExist:
        new_record = ScrapProgress.create(
            chat_id=source_chat_id,
            api_id=config['api_id'],
            message_id=0,
            update_datetime=datetime.now()
        )
        local_scrap_progress[key] = new_record.message_id
        return new_record.message_id

    except Exception as e:
        print(f"Error fetching max source_message_id: {e}")
        return None
        
async def save_scrap_progress(entity_id, message_id):
    key = (entity_id, config['api_id'])
    record = ScrapProgress.get_or_none(
        chat_id=entity_id,
        api_id=config['api_id'],
    )

    if record is None:
        # 不存在时新增
        ScrapProgress.create(
            chat_id=entity_id,
            api_id=config['api_id'],
            message_id=message_id,
            update_datetime=datetime.now()
        )
    elif message_id > record.message_id:
        # 存在且 message_id 更大时才更新
        record.message_id = message_id
        record.update_datetime = datetime.now()
        record.save()


    local_scrap_progress[key] = message_id  # ✅ 同步更新缓存

async def process_user_message(entity, message):
    global current_user_name
    botname = None
    # print(f"{entity.id} {message.text}")
    if message.text:
        try:
            match = re.search(r'\|_kick_\|\s*(.*?)\s*(bot)', message.text, re.IGNORECASE)
            if match:
                botname = match.group(1) + match.group(2)
                await client.send_message(botname, "/start")
                await client.send_message(botname, "[~bot~]")
                await safe_delete_message(message)
                return
        except Exception as e:
                print(f"Error kicking bot: {e} {botname}", flush=True)


        try:
            #  |_ask_|4234@vampire666666666
            match = re.search(r'\|_ask_\|(\d+)@([-\w]+)', message.text, re.IGNORECASE)
            if match:
                # sort_content_id = match.group(1)
                # request_bot_name = match.group(2)
                send_msg = await client.send_message('@ztdthumb011bot', message.text)
                # 删除消息
                await safe_delete_message(send_msg)
                await safe_delete_message(message)
                return

        except Exception as e:
                print(f"Error kicking bot: {e} {botname}", flush=True)

        #  |_join_|QQCyh1N2sMU5ZGQ0

        try:
            inviteurl = None
            match2 = re.search(r'\|_join_\|(.*)', message.text, re.IGNORECASE)
            if match2:
                inviteurl = match2.group(1) 
                print(f"邀请链接: {inviteurl}")
                await join(inviteurl)    #Coniguration
                await safe_delete_message(message)
                return
        except Exception as e:
                print(f"Error livite: {e} {inviteurl}", flush=True)
   

    # # 打印来源
    first_name = getattr(entity, "first_name", "") or ""
    last_name = getattr(entity, "last_name", "") or ""
    entity_title = f"{first_name} {last_name}".strip()
    # # print(f"[User] Message from {entity_title} ({self.entity.id}): {self.message.text}")
    # print(f"\r\n[User] Message from {entity_title} ({entity.id}): {message.id}")

    extra_data = {'app_id': config['api_id'],'config': config}

    # 如果 config 中 is_debug_enabled 有值, 且為 1, 則 pass
    if config.get('bypass_private_check') == 1:
        # print(f"⚠️ bypass_private_check: {config.get('bypass_private_check')}")
        return


    entry = class_map.get(entity.id)
    if entry:
        if current_user_name != entity_title:   
            if config.get('is_debug_enabled') == 1:         
                print(f"👉 处理用户消息 {message.id} 来自: {entity_title} ({entity.id})", flush=True)
            current_user_name = entity_title
        handler_class = entry["handler_class"]
        handler = handler_class(client, entity, message, extra_data)
        handler.is_duplicate_allowed = True
        await handler.handle()
    else:
        
        if config.get('bypass_private_check') == 2:
            
            # print(f"⚠️ bypass_private_check: {config.get('bypass_private_check')}")
            return
        print(f"{config.get('bypass_private_check')}", flush=True)
        # print(f"⚠️ 处理用户消息 {message.id} 来自: {entity.title} ({entity.id})", flush=True)

        handler = HandlerPrivateMessageClass(client, entity, message, extra_data)
        # handler = HandlerNoAction(client, entity, message, extra_data)
        handler.delete_after_process = True
        await handler.handle()


        

       
async def process_group_message(entity, message):
    
    extra_data = {'app_id': config['api_id']}


    # 检测是否是 |_init_|
    if message.text == '|_init_|':
        await invite_bot('luzai01bot', entity)  # 替换为实际的 Bot 用户名
        await invite_bot('luzai01man', entity)  # 替换为实际的 Bot 用户名
        await invite_bot('luzai03bot', entity)  # 替换为实际的 Bot 用户名
        await invite_bot('has_no_access_bot', entity)  # 替换为实际的 Bot 用户名
        await invite_bot('DeletedAcconutBot', entity)  # 替换为实际的 Bot 用户名
        await invite_bot('freebsd66bot', entity)  # 替换为实际的 Bot 用户名
        await safe_delete_message(message)
        await open_chat_history(entity)
        await client.send_message(entity.id, f"entity.id: {str(entity.id)}"  )
        await leave_group(entity)

        return
            

    entry = class_map.get(entity.id)
    if entry:
        handler_class = entry["handler_class"]
        handler = handler_class(client, entity, message, extra_data)
        handler.is_duplicate_allowed = True
        await handler.handle()
    else:
        pass
       


   


async def man_bot_loop():
    last_message_id = 0  # 提前定义，避免 UnboundLocalError
    max_message_id = 1
    async for dialog in client.iter_dialogs():
        try:
            entity = dialog.entity

            # if entity.id != 2210941198:
            #     continue

            # —— 新增：如果是私密／被封禁的频道，直接跳过并加入黑名单
            if isinstance(entity, ChannelForbidden):
                if config.get('is_debug_enabled') == 1:
                    print(f"⚠️ 检测到私密或被封禁频道({entity.id})，跳过处理")
                blacklist_entity_ids.add(entity.id)
                continue

            # ✅ 跳过黑名单
            if await is_blacklisted(entity.id):
                # print(f"🚫 已屏蔽 entity: {entity.id}，跳过处理")
                continue

            current_entity_title = None
            entity_title = getattr(entity, 'title', None)
            if not entity_title:
                first_name = getattr(entity, 'first_name', '') or ''
                last_name = getattr(entity, 'last_name', '') or ''
                entity_title = f"{first_name} {last_name}".strip() or getattr(entity, 'title', f"Unknown entity {entity.id}")



            

            if dialog.unread_count >= 0:
                
                if dialog.is_user:
                    
                    
                    # 如果 config 中 is_debug_enabled 有值, 且為 1, 則 pass
                    if str(config.get('bypass_private_check')) == '1':
                        print(f"⚠️ bypass_private_check: {config.get('bypass_private_check')}")
                        # print(f"⚠️ bypass_private_check: {config.get('bypass_private_check')}")
                        continue

                    

                    current_message = None
                    if str(config.get('bypass_private_check')) != '2':
                        max_message_id = await get_max_source_message_id(entity.id)
                        if max_message_id is None:
                            print(f"❌ P无法获取最大消息 ID，跳过处理 {entity.id}")
                            continue
                    min_id = max_message_id if max_message_id else 1
                    async for message in client.iter_messages(
                        entity, min_id=min_id, limit=99, reverse=True, filter=InputMessagesFilterEmpty()
                    ):
                        current_message = message
                        if current_entity_title != entity_title:
                            
                            current_entity_title = entity_title

                        await process_user_message(entity, message)

                    if current_message:
                        await save_scrap_progress(entity.id, current_message.id)

                    
                    last_message_id = current_message.id if current_message else 0
                    
                    
                else:
                    if config.get('is_debug_enabled') == 1:
                        print(f"👉 当前对话G: {entity_title} ({entity.id})", flush=True)

                    current_message = None
                    max_message_id = await get_max_source_message_id(entity.id)
                    if max_message_id is None:
                        if config.get('is_debug_enabled') == 1:
                            print(f"❌ 无法获取最大消息 ID，跳过处理 {entity.id}")
                        continue
                    min_id = max_message_id if max_message_id else 1

                    try:
                        async for message in client.iter_messages(
                            entity, min_id=min_id, limit=500, reverse=True, filter=InputMessagesFilterEmpty()
                        ):
                            
                            if message.sticker:
                                continue
                            current_message = message
                            if current_entity_title != entity_title:
                                # print(f"[Group]: {current_message.id} 来自: {entity_title} ({entity.id})", flush=True)
                                current_entity_title = entity_title


                            # print(f"当前消息ID(G): {current_message.id}")
                            await process_group_message(entity, message)
                    except ChannelPrivateError as e:
                        print(f"❌ 无法访问频道：{e}")
                        await safe_remove_forbidden(entity)
                    except Exception as e:
                        print(f"{e}", flush=True)
                        # print(f"{message}", flush=True)

                    if_save_progress = True
                    entry = class_map.get(entity.id)
                    if entry:                    
                        if_save_progress = entry.get("save_progress", True)

                    if current_message and if_save_progress:
                        await save_scrap_progress(entity.id, current_message.id)
        except Exception as e:
            print(f"❌ 处理对话 {entity.id} 时出错: {e}", flush=True)
            continue                    
    return last_message_id




# ——把 777000 的新消息“直接转送”为你自己发送的消息——
async def copy_message(client: TelegramClient, target, msg: Message):
    """
    复制文本/媒体到 target（不保留“转发自”标记）。
    """
    try:
        if msg.message and not msg.media:  # 纯文本
            await client.send_message(target, msg.message)
        elif msg.media:  # 含媒体（照片/视频/文件/语音 等）
            await client.send_file(
                target,
                msg.media,
                caption=msg.message or ""
            )
        else:
            # 其它系统/服务型消息（无文本、无媒体）可以忽略或按需处理
            pass
    except FloodWaitError as e:
        # 简单退避：等待 Telegram 要求的秒数后再重试一次
        print(f"[FloodWait] 需等待 {e.seconds}s，准备重试…")
        await asyncio.sleep(e.seconds + 1)
        # 再试一次
        if msg.message and not msg.media:
            await client.send_message(target, msg.message)
        elif msg.media:
            await client.send_file(target, msg.media, caption=msg.message or "")


async def main():
    last_message_id = 0
    phone = config['phone_number']
    print(f"⭐️ 启动 Postman Bot...{phone}", flush=True)
    return
   


    await client.start(config['phone_number'])
    await keep_db_alive()

    me = await client.get_me()

       
    if config.get('is_debug_enabled') == 1:
        print(f'你的用户名: {me.username}',flush=True)
        print(f'你的ID: {me.id}')
        print(f'你的名字: {me.first_name} {me.last_name or ""}')
        print(f'是否是Bot: {me.bot}',flush=True)
 

    start_time = time.time()
    # 显示现在时间
    now = datetime.now()
    print(f"Current: {now.strftime('%Y-%m-%d %H:%M:%S')}",flush=True)

    # await add_contact()

    # try:
    #     await client.edit_2fa(
    #         current_password=OLD_PASSWORD,  # 直接传入旧密码
    #         new_password=NEW_PASSWORD,      # 设置的新密码
    #         hint=HINT
    #     )
    #     print("✅ 2FA 密码已更新")
    # except Exception as e:
    #     print(f"❌ 更新失败: {e}")
    


    # 1. 列出当前帐号所有 active sessions
    auths = await client(GetAuthorizationsRequest())
    print("当前活跃 sessions：")
    for a in auths.authorizations:
       

        if a.hash == 0:
            print(f"✅ 保留 id={a.hash}  device={a.device_model}  platform={a.platform}  ip={a.ip}  date={a.date_created}")
            continue  # 跳过主会话
        elif a.device_model not in WHITELIST:
            try:
                # await client(ResetAuthorizationRequest(hash=a.hash))
                print(f"❌ 已删除 id={a.hash}  device={a.device_model}  platform={a.platform}  ip={a.ip}  date={a.date_created}")
            except Exception as e:
                print(f"删除 {a.hash} 失败: {e}")
        else:
            print(f"✅ 保留 id={a.hash}  device={a.device_model}  platform={a.platform}  ip={a.ip}  date={a.date_created}")
   
    # ——监听 777000 的新消息并即时复制——
    @client.on(events.NewMessage(chats=SOURCE_CHAT_ID))
    async def handler(event: events.NewMessage.Event):
        msg: Message = event.message
        print(f"捕获到 777000 新消息（id={msg.id}）{msg.text or ''}", flush=True)
        # await copy_message(client, TARGET_USER_ID, msg)

    # 长连线轮询，直到被 Ctrl+C 结束
    await client.run_until_disconnected()

    # exit()
    # # 获取来源与目标实体
    # source = await client.get_entity(SOURCE_CHAT_ID)     # 777000
    # target = await client.get_entity(TARGET_USER_ID)     # 7550420493
# await client.send_message(target, msgs[0].text)
    # # 读取最后 3 则（默认新→旧），为了按时间顺序转发，反转一下
    # msgs = await client.get_messages(source, limit=1)
    # msgs = list(reversed(msgs))

    # if not msgs:
    #     print("来源没有可用讯息。")
    #     await client.disconnect()
    #     return

    # # 转发：保留原发送者（forward）
    # try:
    #     # await client.forward_messages(entity=target, messages=msgs, from_peer=source)
    #     await client.send_message(target, msgs[0].text)
    #     print(f"已将 {len(msgs)} 则讯息从 777000 转发给 {TARGET_USER_ID}")
    # except FloodWaitError as e:
    #     print(f"触发限流，请稍后再试，需要等待 {e.seconds} 秒。")
    # finally:
    #     await client.disconnect()


    

    # while (time.time() - start_time) < MAX_PROCESS_TIME:
    #     try:
    #         last_message_id = await asyncio.wait_for(man_bot_loop(), timeout=600)  # 5分钟超时
    #     except asyncio.TimeoutError:
    #         print("⚠️ 任务超时，跳过本轮", flush=True)
    #     await asyncio.sleep(random.randint(5, 10))
       

    # await send_completion_message(last_message_id)

if __name__ == "__main__":
    
    with client:
        
        client.loop.run_until_complete(main())


