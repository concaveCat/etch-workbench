import type { Vec3 } from './types3d';

export type NativeVolume3D = {
  points: Vec3[];
  tetrahedra: [number, number, number, number][];
  materialIds: number[];
};

function fail(message: string): never { throw new Error(message); }

function base64Bytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function headerValue(view: DataView, offset: number, width: number, littleEndian: boolean): number {
  if (width === 4) return view.getUint32(offset, littleEndian);
  const value = view.getBigUint64(offset, littleEndian);
  if (value > BigInt(Number.MAX_SAFE_INTEGER)) fail('VTU block header exceeds the supported array size.');
  return Number(value);
}

async function inflate(bytes: Uint8Array): Promise<Uint8Array> {
  if (typeof DecompressionStream === 'undefined') fail('此浏览器不支持读取 ViennaPS 的 zlib VTU 输出。');
  const copy = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copy).set(bytes);
  const stream = new Blob([copy]).stream().pipeThrough(new DecompressionStream('deflate'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function decodeAppended(encoded: string, offset: number, headerWidth: number, littleEndian: boolean, compressed: boolean): Promise<Uint8Array> {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset >= encoded.length) fail('VTU appended array offset is out of range.');
  const firstChars = Math.ceil(headerWidth / 3) * 4;
  const firstHeader = base64Bytes(encoded.slice(offset, offset + firstChars));
  if (firstHeader.byteLength < headerWidth) fail('VTU array header is truncated.');
  const firstView = new DataView(firstHeader.buffer, firstHeader.byteOffset, firstHeader.byteLength);

  if (!compressed) {
    const length = headerValue(firstView, 0, headerWidth, littleEndian);
    const payloadChars = Math.ceil(length / 3) * 4;
    const payload = base64Bytes(encoded.slice(offset + firstChars, offset + firstChars + payloadChars));
    if (payload.byteLength < length) fail('VTU appended array payload is truncated.');
    return payload.slice(0, length);
  }

  const blockCount = headerValue(firstView, 0, headerWidth, littleEndian);
  if (blockCount === 0) return new Uint8Array();
  if (blockCount > 1_000_000) fail('VTU compressed block count is invalid.');
  const headerBytes = (3 + blockCount) * headerWidth;
  const headerChars = Math.ceil(headerBytes / 3) * 4;
  const header = base64Bytes(encoded.slice(offset, offset + headerChars));
  if (header.byteLength < headerBytes) fail('VTU compressed block table is truncated.');
  const headerView = new DataView(header.buffer, header.byteOffset, header.byteLength);
  const blockSize = headerValue(headerView, headerWidth, headerWidth, littleEndian);
  const lastBlockSize = headerValue(headerView, 2 * headerWidth, headerWidth, littleEndian);
  const compressedSizes = Array.from({ length: blockCount }, (_, index) => headerValue(headerView, (3 + index) * headerWidth, headerWidth, littleEndian));
  const compressedLength = compressedSizes.reduce((total, size) => total + size, 0);
  const payloadChars = Math.ceil(compressedLength / 3) * 4;
  const payload = base64Bytes(encoded.slice(offset + headerChars, offset + headerChars + payloadChars));
  if (payload.byteLength < compressedLength) fail('VTU compressed payload is truncated.');

  const blocks: Uint8Array[] = [];
  let cursor = 0;
  for (let index = 0; index < blockCount; index++) {
    const size = compressedSizes[index];
    const decoded = await inflate(payload.slice(cursor, cursor + size));
    cursor += size;
    const expected = index === blockCount - 1 ? lastBlockSize : blockSize;
    if (decoded.byteLength !== expected) fail('VTU decompressed block size does not match its header.');
    blocks.push(decoded);
  }
  const total = blocks.reduce((sum, block) => sum + block.byteLength, 0);
  const result = new Uint8Array(total);
  cursor = 0;
  for (const block of blocks) { result.set(block, cursor); cursor += block.byteLength; }
  return result;
}

function decodeValues(bytes: Uint8Array, type: string, littleEndian: boolean): number[] {
  const widths: Record<string, number> = {
    Int8: 1, UInt8: 1, Int16: 2, UInt16: 2, Int32: 4, UInt32: 4,
    Int64: 8, UInt64: 8, Float32: 4, Float64: 8,
  };
  const width = widths[type];
  if (!width) fail(`Unsupported VTU scalar type: ${type}`);
  if (bytes.byteLength % width !== 0) fail(`VTU ${type} array has a partial scalar value.`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const values: number[] = [];
  for (let offset = 0; offset < bytes.byteLength; offset += width) {
    let value: number;
    switch (type) {
      case 'Int8': value = view.getInt8(offset); break;
      case 'UInt8': value = view.getUint8(offset); break;
      case 'Int16': value = view.getInt16(offset, littleEndian); break;
      case 'UInt16': value = view.getUint16(offset, littleEndian); break;
      case 'Int32': value = view.getInt32(offset, littleEndian); break;
      case 'UInt32': value = view.getUint32(offset, littleEndian); break;
      case 'Int64': {
        const integer = view.getBigInt64(offset, littleEndian);
        if (integer > BigInt(Number.MAX_SAFE_INTEGER) || integer < BigInt(Number.MIN_SAFE_INTEGER)) fail('VTU Int64 values exceed JavaScript safe integer range.');
        value = Number(integer); break;
      }
      case 'UInt64': {
        const integer = view.getBigUint64(offset, littleEndian);
        if (integer > BigInt(Number.MAX_SAFE_INTEGER)) fail('VTU UInt64 values exceed JavaScript safe integer range.');
        value = Number(integer); break;
      }
      case 'Float32': value = view.getFloat32(offset, littleEndian); break;
      default: value = view.getFloat64(offset, littleEndian); break;
    }
    if (!Number.isFinite(value)) fail(`VTU ${type} array contains a non-finite value.`);
    values.push(value);
  }
  return values;
}

function directChild(element: Element, name: string): Element | null {
  return Array.from(element.children).find(child => child.localName === name) ?? null;
}

function arraysIn(element: Element): Element[] {
  return Array.from(element.children).filter(child => child.localName === 'DataArray');
}

async function readArray(element: Element, appended: string, headerWidth: number, littleEndian: boolean, compressed: boolean): Promise<number[]> {
  const type = element.getAttribute('type');
  if (!type) fail('VTU DataArray is missing its scalar type.');
  const format = element.getAttribute('format') ?? 'ascii';
  if (format === 'appended') {
    const offset = Number(element.getAttribute('offset'));
    return decodeValues(await decodeAppended(appended, offset, headerWidth, littleEndian, compressed), type, littleEndian);
  }
  if (format === 'ascii') {
    const values = (element.textContent ?? '').trim().split(/\s+/).filter(Boolean).map(Number);
    if (values.some(value => !Number.isFinite(value))) fail(`VTU ${element.getAttribute('Name') ?? 'array'} contains invalid ASCII values.`);
    return values;
  }
  fail(`Unsupported VTU DataArray format: ${format}`);
}

function requiredArray(section: Element | null, name: string): Element {
  const array = section && arraysIn(section).find(item => item.getAttribute('Name') === name);
  if (!array) fail(`ViennaPS volume mesh is missing its ${name} array.`);
  return array;
}

export async function parseNativeVtu(text: string): Promise<NativeVolume3D> {
  const document = new DOMParser().parseFromString(text, 'application/xml');
  if (document.querySelector('parsererror')) fail('ViennaPS volume mesh is not valid VTU XML.');
  const root = document.documentElement;
  if (root.localName !== 'VTKFile' || root.getAttribute('type') !== 'UnstructuredGrid') fail('ViennaPS output is not an UnstructuredGrid VTU.');
  const headerType = root.getAttribute('header_type') ?? 'UInt32';
  const headerWidth = headerType === 'UInt64' ? 8 : headerType === 'UInt32' ? 4 : 0;
  if (!headerWidth) fail(`Unsupported VTU header type: ${headerType}`);
  const compressor = root.getAttribute('compressor');
  if (compressor && compressor !== 'vtkZLibDataCompressor') fail(`Unsupported VTU compressor: ${compressor}`);
  const appendedNode = document.querySelector('AppendedData');
  let appended = '';
  if (appendedNode) {
    if ((appendedNode.getAttribute('encoding') ?? 'raw') !== 'base64') fail('Raw binary VTU output cannot be decoded in the browser.');
    appended = (appendedNode.textContent ?? '').replace(/\s/g, '').replace(/^_/, '');
  }

  const piece = document.querySelector('UnstructuredGrid > Piece');
  if (!piece) fail('ViennaPS volume mesh has no UnstructuredGrid Piece.');
  const pointCount = Number(piece.getAttribute('NumberOfPoints'));
  const cellCount = Number(piece.getAttribute('NumberOfCells'));
  if (!Number.isSafeInteger(pointCount) || pointCount < 1 || !Number.isSafeInteger(cellCount) || cellCount < 1) fail('ViennaPS volume mesh has invalid point or cell counts.');
  const pointsSection = directChild(piece, 'Points');
  const pointArray = arraysIn(pointsSection ?? document.createElement('empty'))[0];
  if (!pointArray) fail('ViennaPS volume mesh has no point coordinates.');
  const cellSection = directChild(piece, 'Cells');
  const cellDataSection = directChild(piece, 'CellData');
  const connectivityElement = requiredArray(cellSection, 'connectivity');
  const offsetsElement = requiredArray(cellSection, 'offsets');
  const typesElement = requiredArray(cellSection, 'types');
  const materialElement = requiredArray(cellDataSection, 'Material');
  const littleEndian = root.getAttribute('byte_order') !== 'BigEndian';
  const compressed = Boolean(compressor);
  const [pointValues, connectivity, offsets, types, materialIds] = await Promise.all([
    readArray(pointArray, appended, headerWidth, littleEndian, compressed),
    readArray(connectivityElement, appended, headerWidth, littleEndian, compressed),
    readArray(offsetsElement, appended, headerWidth, littleEndian, compressed),
    readArray(typesElement, appended, headerWidth, littleEndian, compressed),
    readArray(materialElement, appended, headerWidth, littleEndian, compressed),
  ]);
  const components = Number(pointArray.getAttribute('NumberOfComponents') ?? '1');
  if (components !== 3 || pointValues.length !== pointCount * 3) fail('VTU point coordinates do not match NumberOfPoints.');
  if (offsets.length !== cellCount || types.length !== cellCount || materialIds.length !== cellCount) fail('VTU cell topology or material data does not match NumberOfCells.');
  const points: Vec3[] = [];
  for (let index = 0; index < pointValues.length; index += 3) points.push([pointValues[index], pointValues[index + 1], pointValues[index + 2]]);

  const tetrahedra: [number, number, number, number][] = [];
  let previous = 0;
  for (let cell = 0; cell < cellCount; cell++) {
    const end = offsets[cell];
    if (!Number.isSafeInteger(end) || end - previous !== 4 || types[cell] !== 10) fail('ViennaPS volume mesh contains a non-tetrahedral or malformed cell.');
    const nodes = connectivity.slice(previous, end);
    if (nodes.some(index => !Number.isSafeInteger(index) || index < 0 || index >= pointCount) || new Set(nodes).size !== 4) fail('VTU tetrahedron contains invalid point indices.');
    if (!Number.isSafeInteger(materialIds[cell])) fail('VTU cell Material values must be integer material IDs.');
    tetrahedra.push(nodes as [number, number, number, number]);
    previous = end;
  }
  if (previous !== connectivity.length) fail('VTU connectivity length does not match its cell offsets.');
  return { points, tetrahedra, materialIds };
}
