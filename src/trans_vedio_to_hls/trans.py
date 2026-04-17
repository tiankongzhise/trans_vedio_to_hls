#!/usr/bin/env python3
"""
将视频文件转换为 m3u8 (HLS) 格式
- 自动判断编码兼容性：H.264/H.265 + AAC/MP3 直接复制（无损）
- 不兼容时自动转码为 H.264 + AAC，并尽量保持原码率和画质
- 支持 HLS AES-128 加密（可指定已有密钥文件或自动生成）
- 支持通过 TOML 配置文件批量设置参数
- 支持命令行调用和 Python 函数调用两种方式
"""

import subprocess
import sys
import json
import argparse
import os
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, Union

# 尝试导入 TOML 解析库
try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None
        print("警告：未找到 TOML 解析库 (tomli 或 tomllib)，无法使用 --config 选项。请安装 tomli: pip install tomli")

def check_ffmpeg() -> bool:
    """检查 ffmpeg 和 ffprobe 是否可用"""
    for tool in ["ffmpeg", "ffprobe"]:
        try:
            subprocess.run([tool, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            print(f"错误：未找到 {tool}，请先安装 FFmpeg 并确保其在系统 PATH 中。")
            return False
    return True

def load_config(config_path: str) -> Dict[str, Any]:
    """从 TOML 文件加载配置，返回配置字典"""
    if tomllib is None:
        raise RuntimeError("TOML 解析库未安装，无法读取配置文件")
    with open(config_path, "rb") as f:
        return tomllib.load(f)

def expand_path(path_str: str) -> Path:
    # 先展开 ~（Unix 主目录），再展开 %VAR% 形式的环境变量
    expanded = os.path.expanduser(os.path.expandvars(path_str))
    return Path(expanded)

def prepare_hls_encryption(output_dir: Union[str, Path], 
                           key_uri_base: str = "https://yourdomain.com/keys",
                           key_file: Optional[str] = None,
                           iv: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    为HLS加密生成密钥和keyinfo文件。
    
    参数:
        output_dir: 输出目录（m3u8 所在目录）
        key_uri_base: 密钥文件的 URI 前缀（例如 https://example.com/keys）
        key_file: 可选，已有的密钥文件路径。如果提供，则直接使用该文件，不再生成新密钥。
        iv: 可选，IV 十六进制字符串（例如 "0123456789abcdef0123456789abcdef"）。如果不提供，则随机生成。
        
    返回值:
        (key_info_file_path, key_file_path) 或 (None, None) 如果失败
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # 确定密钥文件路径
    if key_file:
        key_file_path = expand_path(key_file)
        if not key_file_path.exists():
            print(f"错误：指定的密钥文件不存在 - {key_file}")
            return None, None
        # 如果密钥文件不在输出目录，则复制到输出目录（保持名称）
        if key_file_path.parent != output_path:
            target_key_path = output_path / key_file_path.name
            try:
                import shutil
                shutil.copy2(key_file_path, target_key_path)
                key_file_path = target_key_path
                print(f"已将密钥文件复制到输出目录: {key_file_path}")
            except Exception as e:
                print(f"复制密钥文件失败: {e}")
                return None, None
        else:
            print(f"使用已有密钥文件: {key_file_path}")
    else:
        # 生成新密钥
        key_file_path = output_path / "enc.key"
        try:
            with open(key_file_path, 'wb') as f:
                subprocess.run(['openssl', 'rand', '16'], stdout=f, check=True)
            print(f"成功生成密钥文件: {key_file_path}")
        except Exception as e:
            print(f"生成密钥失败: {e}")
            return None, None
    
    key_info_file_path = output_path / "enc.keyinfo"
    
    # 处理 IV
    iv_str = ""
    if iv:
        # 用户提供了 IV，直接使用
        iv_str = iv.strip()
        print(f"使用用户提供的 IV: {iv_str}")
    else:
        # 随机生成 IV
        try:
            iv_process = subprocess.run(['openssl', 'rand', '-hex', '16'], capture_output=True, text=True, check=True)
            iv_str = iv_process.stdout.strip()
            print(f"成功生成随机 IV: {iv_str}")
        except Exception as e:
            print(f"生成 IV 失败，将不设置 IV（FFmpeg 会使用默认方法）: {e}")
            iv_str = ""
    
    # 创建 keyinfo 文件
    key_uri = f"{key_uri_base.rstrip('/')}/{key_file_path.name}"
    with open(key_info_file_path, 'w',encoding='utf-8') as f:
        f.write(f"{key_uri}\n")
        f.write(f"{key_file_path.resolve().as_posix()}\n")
        if iv_str:
            f.write(f"{iv_str}\n")
    print(f"成功生成密钥信息文件: {key_info_file_path}")
    
    return str(key_info_file_path), str(key_file_path)

def get_media_info(file_path: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    """使用 ffprobe 获取媒体文件的流信息（编码、码率等）"""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", str(file_path)
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, encoding='utf-8')
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video_info = None
        audio_info = None
        for s in streams:
            if s.get("codec_type") == "video":
                video_info = {
                    "codec": s.get("codec_name", "").lower(),
                    "bitrate": int(s.get("bit_rate", 0)) if s.get("bit_rate") else 0
                }
            elif s.get("codec_type") == "audio":
                audio_info = {
                    "codec": s.get("codec_name", "").lower(),
                    "bitrate": int(s.get("bit_rate", 0)) if s.get("bit_rate") else 0,
                    "sample_rate": s.get("sample_rate", ""),
                    "channels": s.get("channels", 0)
                }
        return video_info, audio_info
    except Exception as e:
        print(f"获取媒体信息失败: {e}")
        return None, None

def is_hls_compatible(video_codec: str, audio_codec: str) -> bool:
    """判断编码是否与 HLS 标准兼容（可直接流复制）"""
    compatible_video = video_codec in ["h264", "hevc", "h265"]
    compatible_audio = audio_codec in ["aac", "mp3"]
    return compatible_video and compatible_audio

def build_ffmpeg_cmd(input_file: str, output_m3u8: str, segment_time: int, hls_list_size: int, start_number: int,
                     video_info: Optional[Dict], audio_info: Optional[Dict], force_transcode: bool = False,
                     key_info_file: Optional[str] = None) -> list:
    """根据输入媒体信息构建 ffmpeg 命令"""
    input_path = Path(input_file)
    output_path = Path(output_m3u8)
    
    # 基础 HLS 参数（输出参数）
    hls_args = [
        "-start_number", str(start_number),
        "-hls_time", str(segment_time),
        "-hls_list_size", str(hls_list_size),
        "-f", "hls"
    ]
    
    # 如果启用了加密，添加 keyinfo 文件参数
    if key_info_file:
        hls_args += ["-hls_key_info_file", key_info_file]
    
    hls_args.append(str(output_path))

    if not force_transcode and video_info and audio_info and is_hls_compatible(video_info["codec"], audio_info["codec"]):
        print("检测到兼容编码（H.264/H.265 + AAC/MP3），使用流复制（无损）")
        # 正确的顺序：-i input -c copy [hls_args]
        cmd = ["ffmpeg", "-i", str(input_path), "-c", "copy"] + hls_args
        return cmd

    # 需要转码
    print("检测到不兼容编码或无法确定，将转码为 H.264 + AAC（尽量保持原码率和画质）")
    video_codec = "libx264"
    audio_codec = "aac"

    # 视频编码参数
    video_opts = []
    if video_info and video_info["bitrate"] and video_info["bitrate"] > 0:
        target_bitrate = max(500, video_info["bitrate"] // 1000)
        video_opts = ["-b:v", f"{target_bitrate}k"]
        print(f"使用目标视频码率: {target_bitrate} kbps (原视频码率: {video_info['bitrate']//1000} kbps)")
    else:
        video_opts = ["-crf", "18", "-preset", "medium"]
        print("无法获取原视频码率，使用 CRF=18 保持高画质")

    # 音频编码参数
    audio_opts = []
    if audio_info and audio_info["bitrate"] and audio_info["bitrate"] > 0:
        audio_bitrate = max(64, audio_info["bitrate"] // 1000)
        audio_opts = ["-b:a", f"{audio_bitrate}k"]
        print(f"使用目标音频码率: {audio_bitrate} kbps (原音频码率: {audio_info['bitrate']//1000} kbps)")
    else:
        audio_opts = ["-b:a", "128k"]
        print("无法获取原音频码率，使用默认 128 kbps")

    # 正确顺序：-i input -c:v libx264 [video_opts] -c:a aac [audio_opts] [hls_args]
    cmd = ["ffmpeg", "-i", str(input_path),
           "-c:v", video_codec] + video_opts + \
           ["-c:a", audio_codec] + audio_opts + hls_args
    return cmd

def convert_to_hls(
    input_file: str,
    output_m3u8: str,
    segment_time: int = 10,
    hls_list_size: int = 0,
    start_number: int = 0,
    force_transcode: bool = False,
    quiet: bool = False,
    encrypt: bool = False,
    key_uri_base: Optional[str] = None,
    key_file: Optional[str] = None,
    iv: Optional[str] = None
) -> bool:
    """
    将视频文件转换为 HLS (m3u8 + ts) 格式。

    参数:
        input_file: 输入视频文件路径
        output_m3u8: 输出的 .m3u8 文件路径
        segment_time: 每个 TS 分片时长（秒），默认 10
        hls_list_size: m3u8 中保存的分片数量，0 表示全部，默认 0
        start_number: 起始分片序号，默认 0
        force_transcode: 强制转码（忽略兼容检测），默认 False
        quiet: 静默模式，不打印详细日志，默认 False
        encrypt: 是否启用 HLS AES-128 加密，默认 False
        key_uri_base: 密钥文件的 URI 前缀（例如 https://example.com/keys），
                      仅当 encrypt=True 时有效，默认使用 "https://yourdomain.com/keys"
        key_file: 可选，已有的密钥文件路径（用于加密）。如果提供，则直接使用该密钥文件。
        iv: 可选，IV 十六进制字符串（例如 "0123456789abcdef0123456789abcdef"），
            如果不提供则随机生成（仅在 encrypt=True 时有效）。

    返回:
        bool: 转换成功返回 True，失败返回 False
    """
    if not quiet:
        print(f"输入文件: {input_file}")
        print(f"输出文件: {output_m3u8}")

    input_path = Path(input_file)
    if not input_path.exists():
        print(f"错误：输入文件不存在 - {input_file}")
        return False

    output_path = Path(output_m3u8)
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 处理加密
    key_info_file = None
    if encrypt:
        if not quiet:
            print("启用 HLS AES-128 加密")
        uri_base = key_uri_base if key_uri_base else "https://yourdomain.com/keys"
        key_info_file, actual_key_file = prepare_hls_encryption(output_dir, uri_base, key_file, iv)
        if not key_info_file:
            print("错误：准备加密密钥失败")
            return False
        if not quiet:
            print(f"密钥信息文件: {key_info_file}")
            print(f"密钥文件: {actual_key_file}")

    # 获取媒体信息
    video_info, audio_info = get_media_info(input_file)
    if not video_info and not quiet:
        print("警告：未检测到视频流，将尝试直接复制或转码")

    # 构建 ffmpeg 命令
    cmd = build_ffmpeg_cmd(
        input_file, output_m3u8, segment_time, hls_list_size, start_number,
        video_info, audio_info, force_transcode, key_info_file
    )

    if not quiet:
        print("开始处理...")
        print("命令:", " ".join(cmd))

    try:
        # 根据 quiet 模式决定是否捕获输出
        if quiet:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        else:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE

        result = subprocess.run(cmd, stdout=stdout, stderr=stderr, text=True, encoding='utf-8')
        if result.returncode != 0:
            if not quiet:
                print("FFmpeg 执行出错：")
                print(result.stderr)
            return False
        if not quiet:
            print("转换成功！")
        return True
    except Exception as e:
        print(f"运行 FFmpeg 时发生异常: {e}")
        return False

# ==================== 命令行入口 ====================

def main():
    parser = argparse.ArgumentParser(description="将视频转换为 m3u8 (HLS)，自动判断是否需要转码，支持 AES-128 加密，支持 TOML 配置文件")
    parser.add_argument("input", help="输入视频文件路径")
    parser.add_argument("output", help="输出 .m3u8 文件路径（例如 output/playlist.m3u8）")
    parser.add_argument("--segment-time", type=int, default=10, help="每个 TS 分片时长（秒），默认 10")
    parser.add_argument("--list-size", type=int, default=0, help="m3u8 中保存的分片数量，0 表示全部，默认 0")
    parser.add_argument("--start-number", type=int, default=0, help="起始分片序号，默认 0")
    parser.add_argument("--force-transcode", action="store_true", help="强制转码（忽略兼容检测）")
    parser.add_argument("--quiet", action="store_true", help="静默模式，不打印详细日志")
    parser.add_argument("--encrypt", action="store_true", help="启用 HLS AES-128 加密")
    parser.add_argument("--key-uri-base", default=None, help="加密密钥的 URI 前缀（例如 https://example.com/keys），默认 https://yourdomain.com/keys")
    parser.add_argument("--key-file", default=None, help="已有的密钥文件路径（用于加密），若不提供则自动生成")
    parser.add_argument("--iv", default=None, help="IV 十六进制字符串（例如 0123456789abcdef0123456789abcdef），若不提供则随机生成")
    parser.add_argument("--config", default=None, help="TOML 配置文件路径，可从文件中读取所有参数（命令行参数优先级更高）")

    args = parser.parse_args()

    # 加载配置文件（如果提供）
    config_params = {}
    if args.config:
        if not os.path.exists(args.config):
            print(f"错误：配置文件不存在 - {args.config}")
            sys.exit(1)
        try:
            config_params = load_config(args.config)
            print(f"已加载配置文件: {args.config}")
        except Exception as e:
            print(f"加载配置文件失败: {e}")
            sys.exit(1)

    # 命令行参数覆盖配置文件
    # 注意：input 和 output 必须从命令行提供，不能从配置文件读取（但也可以支持，这里为简单起见，仍要求命令行提供）
    segment_time = args.segment_time if args.segment_time != 10 else config_params.get("segment_time", 10)
    hls_list_size = args.list_size if args.list_size != 0 else config_params.get("list_size", 0)
    start_number = args.start_number if args.start_number != 0 else config_params.get("start_number", 0)
    force_transcode = args.force_transcode or config_params.get("force_transcode", False)
    quiet = args.quiet or config_params.get("quiet", False)
    encrypt = args.encrypt or config_params.get("encrypt", False)
    key_uri_base = args.key_uri_base if args.key_uri_base is not None else config_params.get("key_uri_base", None)
    key_file = args.key_file if args.key_file is not None else config_params.get("key_file", None)
    iv = args.iv if args.iv is not None else config_params.get("iv", None)

    if not check_ffmpeg():
        sys.exit(1)

    success = convert_to_hls(
        input_file=args.input,
        output_m3u8=args.output,
        segment_time=segment_time,
        hls_list_size=hls_list_size,
        start_number=start_number,
        force_transcode=force_transcode,
        quiet=quiet,
        encrypt=encrypt,
        key_uri_base=key_uri_base,
        key_file=key_file,
        iv=iv
    )

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()