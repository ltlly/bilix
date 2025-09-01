import asyncio
import errno
import os
import random
from functools import wraps
from pathlib import Path

import aiofiles
import httpx
from typing import Union, Sequence, Tuple, List
from bilix.exception import APIError, APIParseError
from bilix.log import logger


async def merge_files(file_list: List[Path], new_path: Path):
    first_file = file_list[0]
    async with aiofiles.open(first_file, "ab") as f:
        for idx in range(1, len(file_list)):
            async with aiofiles.open(file_list[idx], "rb") as fa:
                await f.write(await fa.read())
            os.remove(file_list[idx])
    os.rename(first_file, new_path)


_proxy_pool = None
_proxy_pool_lock = None


def init_proxy_pool(
    clash_api="http://127.0.0.1:9097",
    proxy_server="127.0.0.1",
    proxy_port=7897,
    secret="set-your-secret",
):
    #     pool = ClashProxyPool(
    #     clash_api="http://127.0.0.1:9097",
    #     secret="set-your-secret",
    #     proxy_port=7897,
    #     max_fail=3,
    #     proxy_server="127.0.0.1",
    # )
    global _proxy_pool, _proxy_pool_lock
    from bilix.clashpool import ClashProxyPool

    _proxy_pool = ClashProxyPool(
        clash_api=clash_api,
        proxy_server=proxy_server,
        proxy_port=proxy_port,
        secret=secret,
    )
    import asyncio
    _proxy_pool_lock = asyncio.Lock()


async def req_retry(
    client: httpx.AsyncClient,
    url_or_urls: Union[str, Sequence[str]],
    method="GET",
    follow_redirects=False,
    retry=1000,
    **kwargs,
) -> httpx.Response:
    """Client request with multiple backup urls and retry"""
    global _proxy_pool
    pre_exc = None  # predefine to avoid warning
    use_proxy = False  # 初始不使用代理
    original_kwargs = kwargs.copy()  # 保存原始参数

    for times in range(1 + retry):
        url = url_or_urls if type(url_or_urls) is str else random.choice(url_or_urls)
        try:
            current_kwargs = kwargs.copy()
            if use_proxy and _proxy_pool is not None:
                proxy_addr = f"http://{_proxy_pool.proxy_server}:{_proxy_pool.proxy_port}"
                # 如果当前节点失败次数过多，切换节点（加锁）
                if (
                    not _proxy_pool.current_node
                    or _proxy_pool.node_failures[_proxy_pool.current_node]
                    >= _proxy_pool.max_fail
                ):
                    if _proxy_pool_lock is not None:
                        async with _proxy_pool_lock:
                            _proxy_pool.switch_node()
                    else:
                        _proxy_pool.switch_node()
                async with httpx.AsyncClient(proxy=proxy_addr) as proxy_client:
                    res = await proxy_client.request(
                        method, url, follow_redirects=follow_redirects, **current_kwargs
                    )
            else:
                res = await client.request(
                    method, url, follow_redirects=follow_redirects, **current_kwargs
                )
            res.raise_for_status()

            # 请求成功，重置当前节点的失败计数
            if use_proxy and _proxy_pool is not None and _proxy_pool.current_node:
                _proxy_pool.node_failures[_proxy_pool.current_node] = 0

            return res

        except httpx.TransportError as e:
            msg = f"{method} {e.__class__.__name__} url: {url} {current_kwargs}"
            logger.warning(msg) if times > 0 else logger.debug(msg)
            pre_exc = e
            # 代理模式下遇到连接错误，标记节点失败并切换
            if use_proxy and _proxy_pool is not None and _proxy_pool.current_node:
                if isinstance(e, httpx.ConnectError):
                    _proxy_pool.node_failures[_proxy_pool.current_node] += 1
                    if _proxy_pool_lock is not None:
                        async with _proxy_pool_lock:
                            _proxy_pool.switch_node()
                    else:
                        _proxy_pool.switch_node()
            await asyncio.sleep(0.1 * (times + 1))

        except httpx.HTTPStatusError as e:
            logger.warning(f"{method} {e.__class__.__name__} url: {url} {current_kwargs}")
            pre_exc = e

            if e.response.status_code == 412:
                if not use_proxy and _proxy_pool is not None:
                    # 遇到412时，开始使用代理
                    use_proxy = True
                    logger.info("遇到412错误，切换到代理模式")
                    await asyncio.sleep(1)
                    continue
                elif use_proxy and _proxy_pool is not None and _proxy_pool.current_node:
                    # 使用代理仍遇到412，增加失败计数并切换节点
                    _proxy_pool.node_failures[_proxy_pool.current_node] += 1
                    if _proxy_pool_lock is not None:
                        async with _proxy_pool_lock:
                            _proxy_pool.switch_node()
                    else:
                        _proxy_pool.switch_node()
                    await asyncio.sleep(1)
            else:
                await asyncio.sleep(1.0 * (times + 1))

        except Exception as e:
            logger.warning(f"{method} {e.__class__.__name__} url: {url} {current_kwargs} {e}")
            
            raise e
    logger.error(f"{method} 超过重复次数 {url_or_urls}")
    raise pre_exc


def eclipse_str(s: str, max_len: int = 100):
    if len(s) <= max_len:
        return s
    else:
        half_len = (max_len - 1) // 2
        return f"{s[:half_len]}…{s[-half_len:]}"


def path_check(path: Path, retry: int = 100) -> Tuple[bool, Path]:
    """
    check whether path exist, if filename too long, truncate and return valid path

    :param path: path to check
    :param retry: max retry times
    :return: exist, path
    """
    for times in range(retry):
        try:
            exist = path.exists()
            return exist, path
        except OSError as e:
            if e.errno == errno.ENAMETOOLONG:  # filename too long for os
                if times == 0:
                    logger.warning(
                        f"filename too long for os, truncate will be applied. filename: {path.name}"
                    )
                else:
                    logger.debug(f"filename too long for os {path.name}")
                path = path.with_stem(eclipse_str(path.stem, int(len(path.stem) * 0.8)))
            else:
                raise e
    raise OSError(f"filename too long for os {path.name}")


def raise_api_error(func):
    """Decorator to catch exceptions except APIError and HTTPError and raise APIParseError"""

    @wraps(func)
    async def wrapped(client: httpx.AsyncClient, *args, **kwargs):
        try:
            return await func(client, *args, **kwargs)
        except (APIError, httpx.HTTPError):
            raise
        except Exception as e:
            raise APIParseError(e, func) from e

    return wrapped
