"""最小 PE 解析：只做本工具需要的事（读节表、VA↔文件偏移、按 VA 读字节）。

原本是研究用的 IL2CPP 工具链里一个 3800 行模块的一部分，这里只把 ``PeImage``
单独取出来——解码器只需要它读 GameAssembly.dll 的节表和常量页，不需要方法体映射、
元数据注册等其余能力。

这个类不做任何下载或解包，只读调用方给出的本地文件。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

__all__ = ["PeImage"]

class PeImage:
    def __init__(self, path: Path):
        self.path = path
        self.buf = path.read_bytes()
        pe_offset = self.u32_at_file(0x3C)
        if self.u32_at_file(pe_offset) != 0x00004550:
            raise ValueError(f"not a PE image: {path}")
        coff = pe_offset + 4
        self.section_count = self.u16_at_file(coff + 2)
        optional_size = self.u16_at_file(coff + 16)
        optional = coff + 20
        self._check_file_range(optional, optional_size)
        if optional_size < 32:
            raise ValueError(f"{path}: file offset={optional} expected=at least 32 optional-header bytes actual={optional_size}")
        magic = self.u16_at_file(optional)
        if magic != 0x20B:
            raise ValueError("only PE32+ x64 images are supported")
        self.image_base = self.u64_at_file(optional + 24)
        section_offset = optional + optional_size
        self._check_file_range(section_offset, self.section_count * 40)
        self.sections: list[dict[str, Any]] = []
        for index in range(self.section_count):
            off = section_offset + index * 40
            name = self.buf[off:off + 8].split(b"\0", 1)[0].decode("ascii", errors="replace")
            virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
                "<IIII", self.buf, off + 8
            )
            if raw_size:
                self._check_file_range(raw_pointer, raw_size)
            self.sections.append(
                {
                    "name": name,
                    "virtualAddress": virtual_address,
                    "virtualSize": virtual_size,
                    "rawPointer": raw_pointer,
                    "rawSize": raw_size,
                }
            )

    def u16_at_file(self, offset: int) -> int:
        self._check_file_range(offset, 2)
        return struct.unpack_from("<H", self.buf, offset)[0]

    def u32_at_file(self, offset: int) -> int:
        self._check_file_range(offset, 4)
        return struct.unpack_from("<I", self.buf, offset)[0]

    def u64_at_file(self, offset: int) -> int:
        self._check_file_range(offset, 8)
        return struct.unpack_from("<Q", self.buf, offset)[0]

    def _check_file_range(self, offset: int, size: int) -> None:
        if offset < 0 or size < 0 or offset > len(self.buf) or size > len(self.buf) - offset:
            raise ValueError(
                f"{self.path}: file offset={offset} expected={size} raw bytes "
                f"actual available={max(0, len(self.buf) - max(0, offset))} "
                f"fileLength={len(self.buf)}"
            )

    def file_offset_for_rva(self, rva: int) -> tuple[int | None, str]:
        matches = []
        for section in self.sections:
            start = section["virtualAddress"]
            size = max(section["virtualSize"], section["rawSize"])
            if start <= rva < start + size:
                matches.append(section)
        if len(matches) > 1:
            raise ValueError(f"{self.path}: RVA=0x{rva:x} expected=one section actual={len(matches)}")
        if not matches:
            return None, ""
        section = matches[0]
        relative = rva - section["virtualAddress"]
        offset = section["rawPointer"] + relative
        # A virtual-only tail (BSS) has no on-disk byte. Do not manufacture a
        # file offset that reads the next section's unrelated raw data.
        if relative >= section["rawSize"] or offset >= len(self.buf):
            return None, section["name"]
        return offset, section["name"]

    def file_offset_for_va(self, va: int) -> tuple[int | None, str, int]:
        rva = va - self.image_base
        file_offset, section = self.file_offset_for_rva(rva)
        return file_offset, section, rva

    def u64_at_va(self, va: int) -> int:
        return struct.unpack("<Q", self.bytes_at_va(va, 8))[0]

    def bytes_at_va(self, va: int, size: int) -> bytes:
        offset, name, rva = self.file_offset_for_va(va)
        if offset is None:
            raise ValueError(
                f"{self.path}: VA=0x{va:x} RVA=0x{rva:x} section={name!r} "
                f"expected={size} raw-backed bytes actual=unbacked"
            )
        section = next(s for s in self.sections if s["name"] == name
                       and s["virtualAddress"] <= rva < s["virtualAddress"] + max(s["virtualSize"], s["rawSize"]))
        remaining = min(section["rawSize"] - (rva - section["virtualAddress"]), len(self.buf) - offset)
        if size < 0 or size > remaining:
            raise ValueError(
                f"{self.path}: VA=0x{va:x} file offset={offset} section={name!r} "
                f"expected={size} raw-backed bytes actual available={remaining}"
            )
        return self.buf[offset:offset + size]

    def c_string_at_va(self, va: int, *, limit: int = 512) -> str:
        offset, name, rva = self.file_offset_for_va(va)
        if offset is None:
            return f"<bad-va:0x{va:x}>"
        if limit < 0:
            raise ValueError(f"{self.path}: VA=0x{va:x} expected=nonnegative string limit actual={limit}")
        section = next(s for s in self.sections if s["name"] == name
                       and s["virtualAddress"] <= rva < s["virtualAddress"] + max(s["virtualSize"], s["rawSize"]))
        raw_end = min(len(self.buf), offset + limit, section["rawPointer"] + section["rawSize"])
        end = self.buf.find(b"\0", offset, raw_end)
        if end < 0:
            raise ValueError(f"{self.path}: VA=0x{va:x} expected=NUL within {raw_end - offset} raw bytes actual=unterminated")
        return self.buf[offset:end].decode("utf-8", errors="replace")
