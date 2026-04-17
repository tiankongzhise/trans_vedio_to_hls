import hashlib
import base64

def create_enc_key_file(password:str,salt:bytes,key_length:int=16,iterations:int = 100000):
    # 将密码字符串转换为字节串
    password_bytes = password.encode('utf-8')

    # 使用PBKDF2派生密钥
    # hashlib.pbkdf2_hmac 是Python标准库中实现PBKDF2的函数
    derived_key = hashlib.pbkdf2_hmac(
        'sha256',           # 使用的哈希算法
        password_bytes,     # 密码
        salt,               # 盐值
        iterations,         # 迭代次数
        dklen=key_length    # 期望的密钥长度
    )

    # 将派生出的16字节密钥写入文件
    with open('enc.key', 'wb') as f:
        f.write(derived_key)

    print(f"密钥已生成并保存至 enc.key。")
    # 如果想以十六进制形式查看密钥，可以取消下面一行的注释
    # print(f"密钥 (Hex): {derived_key.hex()}")
def create_salt():
    import secrets
    salt = secrets.token_bytes(16)
    return salt


if __name__ == '__main__':
    # password = 'Tkzs-Hls_0123Pwd456789.'
    # salt = create_salt()
    # salt = b'f,\xc8\x14Y\x14\x80\xd5\xc8.\x17\xc7\x85\x96.T'

    password = 'Tkzs-Hls_0123Pwd456789.'
    salt = b'f,\xc8\x14Y\x14\x80\xd5\xc8.\x17\xc7\x85\x96.T'
    print(f'salt:{salt}')
    create_enc_key_file(password,salt)