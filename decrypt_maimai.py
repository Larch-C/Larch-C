#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐱 麦咪猫语解密工具 v1.0

使用方法:
    python decrypt_maimai.py "喵～♡喵喵～..." [种子]
    
示例:
    python decrypt_maimai.py "$(cat encrypted.txt)" "114514"
"""

import sys
import hashlib

def cat_decrypt(cat_text, seed="114514"):
    """麦咪猫语解密算法"""
    if not cat_text or len(cat_text) % 3 != 0:
        return "❌ 解密失败喵... 密文格式不正确"
        
    # 计算原始长度
    original_length = len(cat_text) // 9
    seed_hash = hashlib.md5(f"{seed}_{original_length}".encode()).digest()
    symbols = ['喵', '～', '♡']
    
    decoded_bytes = []
    for i in range(0, len(cat_text), 3):
        cat_code = cat_text[i:i+3]
        
        # 解码三个符号为字节
        byte_val = 0
        for j, symbol in enumerate(cat_code):
            if symbol in symbols:
                val = symbols.index(symbol)
                if val == 3: val = 2
                byte_val |= (val << (j*2))
            else:
                return f"❌ 发现无效符号: {symbol}"
        
        # 使用种子和位置解密
        pos = len(decoded_bytes)
        offset = (seed_hash[pos % len(seed_hash)] + pos) % 256
        original_byte = byte_val ^ offset
        decoded_bytes.append(original_byte)
    
    try:
        result = bytes(decoded_bytes).decode('utf-8')
        return f"✅ 解密成功！麦咪说: {result}"
    except UnicodeDecodeError:
        return "❌ 解密失败喵... 可能种子不正确或密文损坏"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
        
    encrypted_text = sys.argv[1]
    seed = sys.argv[2] if len(sys.argv) > 2 else "114514"
    
    print("🔓 麦咪猫语解密工具启动...")
    print(f"📝 密文长度: {len(encrypted_text)} 个符号")
    print(f"🔑 使用种子: {seed}")
    print("=" * 50)
    
    result = cat_decrypt(encrypted_text, seed)
    print(result)
    print("=" * 50)
    print("💝 解密工具 by 麦咪 ♡")
