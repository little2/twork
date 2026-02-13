import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, AuthKeyDuplicatedError, RPCError
from telethon.tl.functions.account import UpdateUsernameRequest
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact
import os
import json
import pymysql

# Check if running in a local development environment
if not os.getenv('GITHUB_ACTIONS'):
    from dotenv import load_dotenv
    load_dotenv(dotenv_path='.28817994.get_account.env')
    # load_dotenv(dotenv_path='.20100034.sungfong.env')
    # load_dotenv(dotenv_path='.24066130.decode.env')
    # load_dotenv(dotenv_path='.25506053.jjl.env')
    # load_dotenv(dotenv_path='.25254811.bjd.env')
    # load_dotenv(dotenv_path='.25299903.warehouse.env')
    # .20100034.sungfong.env
    # load_dotenv(dotenv_path='.28817994.luzai.env')
    # load_dotenv(dotenv_path='.env')
    # load_dotenv(dotenv_path='.24690454.queue.env')
    # 
    

config = {
    'api_id': os.getenv('API_ID',''),
    'api_hash': os.getenv('API_HASH',''),
    'phone_number': os.getenv('PHONE_NUMBER',''),
    'setting_chat_id': int(os.getenv('SETTING_CHAT_ID') or 0),
    'setting_thread_id': int(os.getenv('SETTING_THREAD_ID') or 0),
    'setting' : os.getenv('CONFIGURATION', ''),
    'user_session_string' : os.getenv('USER_SESSION_STRING', ''),
}

try:
    setting_json = json.loads(config['setting'])
   
    if isinstance(setting_json, dict):
        config.update(setting_json)  # 將 JSON 鍵值對合併到 config 中
except Exception as e:
    print(f"⚠️ 無法解析 CONFIGURATION：{e}")

print(config, flush=True)

api_id = config['api_id']
api_hash = config['api_hash']
phone_number = config['phone_number']

assert api_id is not None, "❌ 环境变量 API_ID 没有设置！"
assert api_hash is not None, "❌ 环境变量 API_HASH 没有设置！"
assert phone_number is not None, "❌ 环境变量 PHONE_NUMBER 没有设置！"


MYSQL_HOST      = config.get('db_host', os.getenv('MYSQL_DB_HOST', 'localhost'))
MYSQL_USER      = config.get('db_user', os.getenv('MYSQL_DB_USER', ''))
MYSQL_PASSWORD  = config.get('db_password', os.getenv('MYSQL_DB_PASSWORD', ''))
MYSQL_DB        = config.get('db_name', os.getenv('MYSQL_DB_NAME', ''))
MYSQL_DB_PORT   = int(config.get('db_port', os.getenv('MYSQL_DB_PORT', 3306)))


# ================= 2. 初始化 MySQL 连接 =================
mysql_config = {
    'host'      : MYSQL_HOST,
    'user'      : MYSQL_USER,
    'password'  : MYSQL_PASSWORD,
    'database'  : MYSQL_DB,
    "port"      : MYSQL_DB_PORT,
    'charset'   : 'utf8mb4',
    'autocommit': True
}
db = pymysql.connect(**mysql_config)
cursor = db.cursor()


# Get values from environment variables
# api_id = os.getenv('API_ID')
# api_hash = os.getenv('API_HASH')
# phone_number = os.getenv('PHONE_NUMBER')
pw2fa = os.getenv('PW2FA')
newpw = os.getenv('NEW_PW2FA')
session_password = os.getenv('SESSION_PASSWORD')

session_name = str(api_id) + 'session_name'  # Ensure it matches the uploaded session file name

# phone_number = '+13859939226'

session_name = phone_number.replace('+', '').replace(' ', '')  # 确保电话号码格式正确
session_file = session_name + '.session'

#删除旧的会话文件
if os.path.exists(session_file):
    try:
        # os.remove(session_file)
        print(f"已删除旧的会话文件: {session_file}", flush=True)
    except FileNotFoundError:
        print("删除旧的会话文件时未找到文件。", flush=True)

# Create the client
client = TelegramClient(session_name, api_id, api_hash)

async def login():
    try:
        # Send verification code and start the login process
        print("Sending verification code to the specified phone number...", flush=True)
        await client.send_code_request(phone_number)

        # User inputs the received verification code
        code = input('Please enter the code you received(a): ')  
       
        # Use phone number and verification code to log in
        return await client.sign_in(phone=phone_number, password=pw2fa, code=code)

    except SessionPasswordNeededError:
        # Handle two-factor authentication
        print("Two-factor authentication password is required", flush=True)
        await client.sign_in(password=pw2fa)

    except RPCError as e:
        # Capture RPC error and display detailed error message
        print(f"Failed to send verification request, error: {e}", flush=True)        


async def encrypt_session_file(input_file, output_file, password):
    try:
        # 构建 OpenSSL 加密命令
        openssl_command = [
            'openssl', 'aes-256-cbc', '-pbkdf2', '-salt',
            '-in', input_file, '-out', output_file,
            '-pass', f'pass:{password}'
        ]

        # 使用 asyncio.create_subprocess_exec 异步执行命令
        process = await asyncio.create_subprocess_exec(
            *openssl_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # 等待进程完成并获取输出
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            print(f"Encryption successful. Encrypted file saved as {output_file}")
        else:
            print(f"Encryption failed: {stderr.decode()}")

    except Exception as e:
        print(f"Encryption failed with exception: {str(e)}")

async def main():
    try:
        # Attempt to start the client
        print("Attempting to start the client...", flush=True)
        await client.connect()
    except AuthKeyDuplicatedError:
        # Capture AuthKeyDuplicatedError, delete the old session file and re-login
        print("Detected duplicate authorization key, deleting old session file and retrying login...", flush=True)
        await client.disconnect()  # Disconnect the client
        client.session.close()  # Ensure the session is closed
        try:
            if os.path.exists(session_file):
                # os.remove(session_file)
                print(f"已删除会话文件: {session_file}", flush=True)
            if os.path.exists(session_file + ".enc"):
                os.remove(session_file + ".enc")
                print(f"已删除加密的会话文件: {session_file}.enc", flush=True)
        except FileNotFoundError:
            print("删除会话文件时未找到文件。", flush=True)
        await asyncio.sleep(1)  # 间隔1秒

        await client.connect()
        # Reconnect after deleting the old session
        #
        # 检查客户端是否已连接
        if not client.is_connected():
            print("客户端连接失败，请检查网络连接或 API 密钥配置。", flush=True)
            return

    # Check if the user is already authorized
    if not await client.is_user_authorized():
        print(f"User is not authorized, starting the login process...{phone_number}", flush=True)
        result = await login()
        print(f"Login result: {result}", flush=True)

        stringsession = StringSession.save(client.session)
        print("\n✅ 以下是你的 StringSession（可写入 .env）\n")
        print("USER_SESSION_STRING=" + stringsession)

    else:
    
        print("User is already authorized, no need to log in again", flush=True)
        stringsession = StringSession.save(client.session)
        print("\n✅ 以下是你的 StringSession（可写入 .env）\n")
        print("USER_SESSION_STRING=" + stringsession)


    
    # try:
    #     phone_number2 = phone_number.replace('+', 'p_').replace(' ', '')  # 确保电话号码格式正确
    #     await client(UpdateUsernameRequest(phone_number2))  # 设置空字符串即为移除
    #     print("用户名已成功变更。")
    # except Exception as e:
    #     print(f"用户名变更失败：{e}")

    me = await client.get_me()

       
    
    print(f'你的用户名: {me.username}',flush=True)
    print(f'你的ID: {me.id}')
    print(f'你的名字: {me.first_name} {me.last_name or ""}')
    print(f'是否是Bot: {me.bot}',flush=True)

    # print(f"\n\nopenssl aes-256-cbc -pbkdf2 -salt -in {session_file} -out {session_file}.enc -pass pass:{session_password}\n\n")
    # await encrypt_session_file(session_file, session_file+".enc", session_password)

    # ========== 更新 bot 表 ==========
    bot_id = me.id
    bot_token = stringsession
    bot_name = me.username
    user_id = me.id
    bot_root = me.username
    bot_title = f"{me.first_name or ''}{me.last_name or ''}"
    work_status = "free"
    memo = ""  # Add a memo variable or value here
    phone = phone_number

    try:
        cursor.execute("""
            INSERT INTO bot (
                bot_id, bot_token, bot_name, user_id, bot_root, bot_title, work_status,phone,memo
            ) VALUES (%s, %s, %s, %s, %s, %s, %s,%s,%s)
            ON DUPLICATE KEY UPDATE
                bot_token = VALUES(bot_token),
                bot_name = VALUES(bot_name),
                user_id = VALUES(user_id),
                bot_root = VALUES(bot_root),
                bot_title = VALUES(bot_title),
                phone = VALUES(phone),
                memo      = CONCAT(IFNULL(memo, ''), '\n', VALUES(memo))
        """, (bot_id, bot_token, bot_name, user_id, bot_root, bot_title, work_status, phone, memo))
        print("✅ bot 信息已写入或更新数据库")

        try:
            await client.edit_2fa(
                current_password=pw2fa,  # 直接传入旧密码
                new_password=newpw,      # 设置的新密码
                hint="HINT"
            )
            print("✅ 2FA 密码已更新")
        except Exception as e:
            print(f"❌ 更新失败: {e}")

        # 构造一个要导入的联系人
        contact = InputPhoneContact(
            client_id=0, 
            phone="+447447471403", 
            first_name="Vampire", 
            last_name=""
        )

        TARGET_USER_ID = 8150238704           # 接收者 user_id（整数）

        result = await client(ImportContactsRequest([contact]))
        print("导入结果:", result)
        target = await client.get_entity(TARGET_USER_ID)     # 7550420493


        me = await client.get_me()
        await client.send_message(target, f"[RESET] 你好, 我是 <code>{me.id}</code> - {me.first_name} {me.last_name or ''} +{me.phone} \nrestricted={me.restricted}\nscam={me.scam}\nfake={me.fake}",parse_mode="HTML")

        from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
        WHITELIST = {
            "Redmi Redmi K40",                       # PC 64bit Android
            "XiaomiM2012K11AC",     # XiaomiM2012K11AC
            "PC 64bit",     # PC 64bit
            "Oppo Find X7",
            "OPPOPHZ110",
            "MacBook Pro",
            "U36JC"
        }

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
                    print(f"❌ 已删除 id={a.hash}  device_model={a.device_model}  platform={a.platform}  ip={a.ip}  date={a.date_created}")
                except Exception as e:
                    print(f"删除 {a.hash} 失败: {e}")
            else:
                print(f"✅ 保留 id={a.hash}  device_model={a.device_model}  platform={a.platform}  ip={a.ip}  date={a.date_created}")
    

    except Exception as e:
        print(f"❌ 写入数据库失败: {e}", flush=True)







# Explicitly control client startup process instead of using `with client:`
if __name__ == '__main__':
    try:
        # client.start(phone=phone_number)
        client.loop.run_until_complete(main())
    except AuthKeyDuplicatedError:
        print("Handling AuthKeyDuplicatedError, deleting old session file...", flush=True)
        client.disconnect()  # Disconnect before deleting session file
        client.session.close()  # Ensure session file is released
        if os.path.exists(session_file):
            os.remove(session_file)
        print("Session file deleted, please rerun the program to verify...", flush=True)
    except Exception as e:
        print(f"An exception occurred: {e}", flush=True)
