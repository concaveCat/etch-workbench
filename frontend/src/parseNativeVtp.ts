export type VtpOutputRef = {
  fileName: string;
  pointCount: number | null;
  cellCounts: Record<string, number>;
  fields: { name: string; association: 'pointData' | 'cellData'; components: number; tuples: number; nativeType?: string | null }[];
};

export type NativeField = {
  name: string;
  association: 'pointData' | 'cellData';
  components: number;
  tuples: number;
  nativeType: string;
  values: number[];
};

export type ParsedOutput = { fields: NativeField[]; pointCount: number };

type DecodedArray = { values: number[]; type: string; components: number; tuples: number };

function fail(message: string): never { throw new Error(message); }

function base64Bytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function safeHeaderValue(view: DataView, offset: number, width: number, littleEndian: boolean): number {
  if (width === 4) return view.getUint32(offset, littleEndian);
  const value = view.getBigUint64(offset, littleEndian);
  if (value > BigInt(Number.MAX_SAFE_INTEGER)) fail('VTP block header exceeds the supported array size.');
  return Number(value);
}

function decodeScalarValues(bytes: Uint8Array, type: string, littleEndian: boolean): number[] {
  const widths: Record<string, number> = {
    Int8: 1, UInt8: 1, Char: 1, UnsignedChar: 1,
    Int16: 2, UInt16: 2, Short: 2, UnsignedShort: 2,
    Int32: 4, UInt32: 4, Int: 4, UnsignedInt: 4, Float32: 4, Float64: 8, Double: 8,
    Int64: 8, UInt64: 8, Long: 8, UnsignedLong: 8,
  };
  const width = widths[type];
  if (!width) fail(`Unsupported VTP scalar type: ${type}`);
  if (bytes.byteLength % width !== 0) fail(`VTP ${type} array has a partial scalar value.`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const values: number[] = [];
  for (let offset = 0; offset < bytes.byteLength; offset += width) {
    let value: number;
    switch (type) {
      case 'Int8': case 'Char': value = view.getInt8(offset); break;
      case 'UInt8': case 'UnsignedChar': value = view.getUint8(offset); break;
      case 'Int16': case 'Short': value = view.getInt16(offset, littleEndian); break;
      case 'UInt16': case 'UnsignedShort': value = view.getUint16(offset, littleEndian); break;
      case 'Int32': case 'Int': value = view.getInt32(offset, littleEndian); break;
      case 'UInt32': case 'UnsignedInt': value = view.getUint32(offset, littleEndian); break;
      case 'Int64': case 'Long': {
        const integer = view.getBigInt64(offset, littleEndian);
        if (integer > BigInt(Number.MAX_SAFE_INTEGER) || integer < BigInt(Number.MIN_SAFE_INTEGER)) fail('VTP Int64 values exceed JavaScript safe integer range.');
        value = Number(integer); break;
      }
      case 'UInt64': case 'UnsignedLong': {
        const integer = view.getBigUint64(offset, littleEndian);
        if (integer > BigInt(Number.MAX_SAFE_INTEGER)) fail('VTP UInt64 values exceed JavaScript safe integer range.');
        value = Number(integer); break;
      }
      case 'Float32': value = view.getFloat32(offset, littleEndian); break;
      default: value = view.getFloat64(offset, littleEndian); break;
    }
    values.push(value);
  }
  return values;
}

async function inflate(bytes: Uint8Array): Promise<Uint8Array> {
  if (typeof DecompressionStream === 'undefined') fail('此浏览器不支持读取 ViennaLS 的 zlib VTP 输出。');
  const copy = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copy).set(bytes);
  const stream = new Blob([copy]).stream().pipeThrough(new DecompressionStream('deflate'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function decodeAppended(compressedText: string, offset: number, headerWidth: number, littleEndian: boolean, isCompressed: boolean): Promise<Uint8Array> {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset >= compressedText.length) fail('VTP appended array offset is out of range.');
  const encoded = compressedText.slice(offset);
  if (!isCompressed) {
    const headerSize = headerWidth;
    const header = base64Bytes(encoded.slice(0, Math.ceil(headerSize / 3) * 4));
    if (header.byteLength < headerWidth) fail('VTP array length header is truncated.');
    const dataLength = safeHeaderValue(new DataView(header.buffer, header.byteOffset, header.byteLength), 0, headerWidth, littleEndian);
    const payloadChars = Math.ceil(dataLength / 3) * 4;
    const payload = base64Bytes(encoded.slice(Math.ceil(headerSize / 3) * 4, Math.ceil(headerSize / 3) * 4 + payloadChars));
    if (payload.byteLength < dataLength) fail('VTP appended array payload is truncated.');
    return payload.slice(0, dataLength);
  }

  const firstHeaderChars = Math.ceil(headerWidth / 3) * 4;
  const firstHeaderBytes = base64Bytes(encoded.slice(0, firstHeaderChars));
  if (firstHeaderBytes.byteLength < headerWidth) fail('VTP compressed block header is truncated.');
  const nBlocks = safeHeaderValue(new DataView(firstHeaderBytes.buffer, firstHeaderBytes.byteOffset, firstHeaderBytes.byteLength), 0, headerWidth, littleEndian);
  if (nBlocks === 0) return new Uint8Array();
  if (nBlocks > 1_000_000) fail('VTP compressed block count is invalid.');
  const headerBytes = (3 + nBlocks) * headerWidth;
  const headerChars = Math.ceil(headerBytes / 3) * 4;
  const header = base64Bytes(encoded.slice(0, headerChars));
  if (header.byteLength < headerBytes) fail('VTP compressed block table is truncated.');
  const headerView = new DataView(header.buffer, header.byteOffset, header.byteLength);
  const blockSize = safeHeaderValue(headerView, headerWidth, headerWidth, littleEndian);
  const finalBlockSize = safeHeaderValue(headerView, 2 * headerWidth, headerWidth, littleEndian);
  const compressedSizes = Array.from({ length: nBlocks }, (_, index) => safeHeaderValue(headerView, (3 + index) * headerWidth, headerWidth, littleEndian));
  const compressedLength = compressedSizes.reduce((total, value) => total + value, 0);
  const compressedChars = Math.ceil(compressedLength / 3) * 4;
  const compressed = base64Bytes(encoded.slice(headerChars, headerChars + compressedChars));
  if (compressed.byteLength < compressedLength) fail('VTP compressed payload is truncated.');
  const result: Uint8Array[] = [];
  let cursor = 0;
  for (let index = 0; index < nBlocks; index++) {
    const compressedBlock = compressed.slice(cursor, cursor + compressedSizes[index]);
    cursor += compressedSizes[index];
    const decoded = await inflate(compressedBlock);
    const expectedSize = index === nBlocks - 1 ? finalBlockSize : blockSize;
    if (decoded.byteLength !== expectedSize) fail('VTP decompressed block size does not match its header.');
    result.push(decoded);
  }
  const total = result.reduce((size, block) => size + block.byteLength, 0);
  const combined = new Uint8Array(total);
  cursor = 0;
  for (const block of result) { combined.set(block, cursor); cursor += block.byteLength; }
  return combined;
}

function directChild(element: Element, name: string): Element | null {
  return Array.from(element.children).find(child => child.localName === name) ?? null;
}

function arraysIn(element: Element): Element[] {
  return Array.from(element.children).filter(child => child.localName === 'DataArray');
}

async function decodeArray(element: Element, appended: string, headerWidth: number, littleEndian: boolean, isCompressed: boolean): Promise<DecodedArray> {
  const type = element.getAttribute('type');
  if (!type) fail('VTP DataArray is missing its scalar type.');
  const components = Number(element.getAttribute('NumberOfComponents') ?? '1');
  if (!Number.isSafeInteger(components) || components < 1) fail(`VTP ${element.getAttribute('Name') ?? 'array'} has an invalid component count.`);
  const format = element.getAttribute('format') ?? 'ascii';
  let values: number[];
  if (format === 'appended') {
    const offset = Number(element.getAttribute('offset'));
    const bytes = await decodeAppended(appended, offset, headerWidth, littleEndian, isCompressed);
    values = decodeScalarValues(bytes, type, littleEndian);
  } else if (format === 'ascii') {
    values = (element.textContent ?? '').trim().split(/\s+/).filter(Boolean).map(Number);
    if (values.some(value => !Number.isFinite(value))) fail(`VTP ${element.getAttribute('Name') ?? 'array'} contains invalid ASCII values.`);
  } else {
    fail(`Unsupported VTP DataArray format: ${format}`);
  }
  if (values.length % components !== 0) fail(`VTP ${element.getAttribute('Name') ?? 'array'} has incomplete tuples.`);
  return { values, type, components, tuples: values.length / components };
}

function manifestCellCounts(piece: Element): Record<string, number> {
  const aliases: Record<string, string> = { Verts: 'verts', Lines: 'lines', Strips: 'strips', Polys: 'polys' };
  return Object.fromEntries(Object.entries(aliases).map(([xmlName, key]) => {
    const count = Number(piece.getAttribute(`NumberOf${xmlName}`) ?? '0');
    if (!Number.isSafeInteger(count) || count < 0) fail(`VTP Piece has an invalid ${xmlName} count.`);
    return [key, count];
  }));
}

export async function parseNativeVtp(text: string, reference: VtpOutputRef): Promise<ParsedOutput> {
  const document = new DOMParser().parseFromString(text, 'application/xml');
  if (document.querySelector('parsererror')) fail('ViennaLS 输出不是有效的 VTP XML。');
  const root = document.documentElement;
  if (root.localName !== 'VTKFile' || root.getAttribute('type') !== 'PolyData') fail('ViennaLS 中间输出不是 PolyData VTP。');
  const littleEndian = root.getAttribute('byte_order') !== 'BigEndian';
  const headerType = root.getAttribute('header_type') ?? 'UInt32';
  const headerWidth = headerType === 'UInt64' ? 8 : headerType === 'UInt32' ? 4 : 0;
  if (!headerWidth) fail(`Unsupported VTP header type: ${headerType}`);
  const compressor = root.getAttribute('compressor');
  if (compressor && compressor !== 'vtkZLibDataCompressor') fail(`Unsupported VTP compressor: ${compressor}`);
  const appendedNode = document.querySelector('AppendedData');
  let appended = '';
  if (appendedNode) {
    if ((appendedNode.getAttribute('encoding') ?? 'raw') !== 'base64') fail('Raw binary VTP output cannot be decoded in the browser.');
    appended = (appendedNode.textContent ?? '').replace(/\s/g, '').replace(/^_/, '');
  }
  const piece = document.querySelector('PolyData > Piece');
  if (!piece) fail('VTP output has no PolyData Piece.');
  const pointCount = Number(piece.getAttribute('NumberOfPoints'));
  if (!Number.isSafeInteger(pointCount) || pointCount < 0) fail('VTP output has an invalid point count.');
  const cellCounts = manifestCellCounts(piece);
  if (reference.pointCount !== null && reference.pointCount !== pointCount) fail('VTP point count does not match the output manifest.');
  for (const [name, key] of Object.entries({ Verts: 'verts', Lines: 'lines', Strips: 'strips', Polys: 'polys' })) {
    const count = cellCounts[key];
    const manifestCount = reference.cellCounts[name] ?? reference.cellCounts[key];
    if (manifestCount !== undefined && manifestCount !== count) fail(`VTP ${name} count does not match the output manifest.`);
  }

  const getSection = (name: string) => directChild(piece, name);
  const decoded = new Map<Element, DecodedArray>();
  for (const sectionName of ['PointData', 'CellData', 'Points', 'Verts', 'Lines', 'Strips', 'Polys']) {
    const section = getSection(sectionName);
    if (!section) continue;
    for (const array of arraysIn(section)) decoded.set(array, await decodeArray(array, appended, headerWidth, littleEndian, Boolean(compressor)));
  }

  const points = directChild(piece, 'Points');
  const pointsArray = points ? arraysIn(points)[0] : undefined;
  if (!pointsArray) fail('VTP output has no point coordinates.');
  const coordinates = decoded.get(pointsArray)!;
  if (coordinates.components !== 3 || coordinates.tuples !== pointCount) fail('VTP point coordinates do not match NumberOfPoints.');
  if (coordinates.values.some(value => !Number.isFinite(value))) fail('VTP point coordinates contain non-finite values.');

  for (const [xmlName, key] of Object.entries({ Verts: 'verts', Lines: 'lines', Strips: 'strips', Polys: 'polys' })) {
    const topology = directChild(piece, xmlName);
    if (!topology) continue;
    const connectivityArray = arraysIn(topology).find(array => array.getAttribute('Name') === 'connectivity');
    const offsetsArray = arraysIn(topology).find(array => array.getAttribute('Name') === 'offsets');
    if (!connectivityArray || !offsetsArray) fail(`VTP ${xmlName} topology is missing connectivity or offsets.`);
    const connectivity = decoded.get(connectivityArray)!.values;
    const offsets = decoded.get(offsetsArray)!.values;
    if (offsets.length !== cellCounts[key]) fail(`VTP ${xmlName} offsets do not match its declared cell count.`);
    let previous = 0;
    for (const offset of offsets) {
      if (!Number.isSafeInteger(offset) || offset <= previous || offset > connectivity.length) fail(`VTP ${xmlName} has invalid cell offsets.`);
      previous = offset;
    }
    if ((offsets.at(-1) ?? 0) !== connectivity.length) fail(`VTP ${xmlName} connectivity length does not match its offsets.`);
    if (connectivity.some(index => !Number.isSafeInteger(index) || index < 0 || index >= pointCount)) fail(`VTP ${xmlName} contains an out-of-range point index.`);
  }

  const totalCellCount = Object.values(cellCounts).reduce((total, count) => total + count, 0);
  const fields: NativeField[] = [];
  for (const [sectionName, association] of [['PointData', 'pointData'], ['CellData', 'cellData']] as const) {
    const section = getSection(sectionName);
    if (!section) continue;
    const expectedTuples = association === 'pointData' ? pointCount : totalCellCount;
    for (const array of arraysIn(section)) {
      const name = array.getAttribute('Name');
      if (!name) fail(`VTP ${association} array is missing its name.`);
      const value = decoded.get(array)!;
      if (value.tuples !== expectedTuples) fail(`VTP ${name} tuple count does not match its ${association} mesh association.`);
      fields.push({ name, association, components: value.components, tuples: value.tuples, nativeType: value.type, values: value.values });
    }
  }

  const expectedFields = reference.fields;
  if (expectedFields.length !== fields.length) fail('VTP field count does not match the output manifest.');
  for (const expected of expectedFields) {
    const actual = fields.find(field => field.name === expected.name && field.association === expected.association);
    if (!actual || actual.components !== expected.components || actual.tuples !== expected.tuples || (expected.nativeType && actual.nativeType !== expected.nativeType)) {
      fail(`VTP field ${expected.name} does not match the output manifest.`);
    }
  }
  return { fields, pointCount };
}
