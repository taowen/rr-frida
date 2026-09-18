'use strict';
// Module identity and byte helpers.
//
// A recording is only meaningful for the exact build it came from, so the
// agent pins the run to one of:
//
//   build_id  the GNU Build ID note read from the loaded image
//   sha256    the hash of the module's backing file
//
// readBuildId parses the ELF program headers and note segment directly rather
// than trusting a symbol or a filename.

function align4(value) {
  return (value + 3) & ~3;
}

function byteHex(address, size) {
  const bytes = new Uint8Array(address.readByteArray(size));
  let result = '';
  for (const value of bytes) result += value.toString(16).padStart(2, '0');
  return result;
}

function readBuildId(module) {
  const base = module.base;
  if (byteHex(base, 4) !== '7f454c46' || base.add(4).readU8() !== 2
      || base.add(5).readU8() !== 1) {
    throw new Error('module is not a little-endian ELF64 image');
  }
  const phoff = base.add(0x20).readU64().toNumber();
  const phentsize = base.add(0x36).readU16();
  const phnum = base.add(0x38).readU16();
  for (let index = 0; index < phnum; index++) {
    const phdr = base.add(phoff + index * phentsize);
    if (phdr.readU32() !== 4) continue;   // PT_NOTE
    const start = base.add(phdr.add(0x10).readU64().toNumber());
    const size = phdr.add(0x28).readU64().toNumber();
    let offset = 0;
    while (offset + 12 <= size) {
      const note = start.add(offset);
      const nameSize = note.readU32();
      const descSize = note.add(4).readU32();
      const type = note.add(8).readU32();
      const total = 12 + align4(nameSize) + align4(descSize);
      if (total <= 12 || offset + total > size) break;
      const name = nameSize === 0 ? '' : byteHex(note.add(12), nameSize);
      if (type === 3 && name.startsWith('474e5500')) {   // NT_GNU_BUILD_ID, "GNU\0"
        return byteHex(note.add(12 + align4(nameSize)), descSize);
      }
      offset += total;
    }
  }
  throw new Error('GNU Build ID note not found');
}

function verifyModuleIdentity(module, expected) {
  if (expected.build_id !== undefined) {
    const buildId = readBuildId(module);
    if (buildId !== expected.build_id) {
      throw new Error('Build ID mismatch: expected ' + expected.build_id + ', got ' + buildId);
    }
    return {build_id: buildId};
  }
  if (typeof expected.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(expected.sha256)) {
    throw new Error('probe set requires a pinned Build ID or SHA-256');
  }
  const bytes = File.readAllBytes(module.path);
  const digest = Checksum.compute('sha256', bytes);
  if (digest !== expected.sha256) {
    throw new Error('module SHA-256 mismatch: expected ' + expected.sha256 + ', got ' + digest);
  }
  return {sha256: digest, file_size: bytes.byteLength};
}
