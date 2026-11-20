//! Pure-Rust RFC 8032 Ed25519 signature verification using `sha2::Sha512`.
//! Keeps `Cargo.lock` unchanged with zero external crypto dependencies.

use sha2::{Digest, Sha512};

type Gf = [i64; 16];

const GF0: Gf = [0; 16];
const GF1: Gf = [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];

const D: Gf = [
    0x78a3, 0x1359, 0x4dca, 0x75eb, 0xd8ab, 0x4141, 0x0a4d, 0x0070,
    0xe898, 0x7779, 0x4079, 0x8cc7, 0xfe73, 0x2b6f, 0x6cee, 0x5203,
];
const D2: Gf = [
    0xf159, 0x26b2, 0x9b94, 0xebd6, 0xb156, 0x8283, 0x149a, 0x00e0,
    0xd130, 0xeef3, 0x80f2, 0x198e, 0xfce7, 0x56df, 0xd9dc, 0x2406,
];
const X: Gf = [
    0xd51a, 0x8f25, 0x2d60, 0xc956, 0xa7b2, 0x9525, 0xc760, 0x692c,
    0xdc5c, 0xfdd6, 0xe231, 0xc0a4, 0x53fe, 0xcd6e, 0x36d3, 0x2169,
];
const Y: Gf = [
    0x6658, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666,
    0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666,
];
const I: Gf = [
    0xa0b0, 0x4a0e, 0x1b27, 0xc4ee, 0xe478, 0xad2f, 0x1806, 0x2f43,
    0xd7a7, 0x3dfb, 0x0099, 0x2b4d, 0xdf0b, 0x4fc1, 0x2480, 0x2b83,
];

// Group order L in little-endian 32 bytes.
const L: [u8; 32] = [
    0xed, 0xd3, 0xf5, 0x5c, 0x1a, 0x63, 0x12, 0x58,
    0xd6, 0x9c, 0xf7, 0xa2, 0xde, 0xf9, 0xde, 0x14,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10,
];

fn add(a: &Gf, b: &Gf) -> Gf {
    let mut o = [0i64; 16];
    for i in 0..16 {
        o[i] = a[i] + b[i];
    }
    o
}

fn sub(a: &Gf, b: &Gf) -> Gf {
    let mut o = [0i64; 16];
    for i in 0..16 {
        o[i] = a[i] - b[i];
    }
    o
}

fn car25519(o: &mut Gf) {
    for i in 0..16 {
        o[i] += 1 << 16;
        let c = o[i] >> 16;
        o[(i + 1) * usize::from(i < 15)] += c - 1 + 37 * (c - 1) * i64::from(i == 15);
        o[i] -= c << 16;
    }
}

fn mul(a: &Gf, b: &Gf) -> Gf {
    let mut t = [0i64; 31];
    for i in 0..16 {
        for j in 0..16 {
            t[i + j] += a[i] * b[j];
        }
    }
    for i in 0..15 {
        t[i] += 38 * t[i + 16];
    }
    let mut o = [0i64; 16];
    o.copy_from_slice(&t[..16]);
    car25519(&mut o);
    car25519(&mut o);
    o
}

fn sq(a: &Gf) -> Gf {
    mul(a, a)
}

fn sel25519(p: &mut Gf, q: &mut Gf, b: i64) {
    let c = !(b - 1);
    for i in 0..16 {
        let t = c & (p[i] ^ q[i]);
        p[i] ^= t;
        q[i] ^= t;
    }
}

fn pack25519(n: &Gf) -> [u8; 32] {
    let mut m = GF0;
    let mut t = *n;
    car25519(&mut t);
    car25519(&mut t);
    car25519(&mut t);
    for _ in 0..2 {
        m[0] = t[0] - 0xffed;
        for i in 1..15 {
            m[i] = t[i] - 0xffff - ((m[i - 1] >> 16) & 1);
            m[i - 1] &= 0xffff;
        }
        m[15] = t[15] - 0x7fff - ((m[14] >> 16) & 1);
        let b = (m[15] >> 16) & 1;
        m[14] &= 0xffff;
        sel25519(&mut t, &mut m, 1 - b);
    }
    let mut o = [0u8; 32];
    for i in 0..16 {
        o[2 * i] = (t[i] & 0xff) as u8;
        o[2 * i + 1] = (t[i] >> 8) as u8;
    }
    o
}

fn neq25519(a: &Gf, b: &Gf) -> bool {
    pack25519(a) != pack25519(b)
}

fn par25519(a: &Gf) -> u8 {
    pack25519(a)[0] & 1
}

fn unpack25519(n: &[u8; 32]) -> Gf {
    let mut o = [0i64; 16];
    for i in 0..16 {
        o[i] = i64::from(n[2 * i]) + (i64::from(n[2 * i + 1]) << 8);
    }
    o[15] &= 0x7fff;
    o
}

fn inv25519(i: &Gf) -> Gf {
    let mut c = *i;
    for a in (0..=253).rev() {
        c = sq(&c);
        if a != 2 && a != 4 {
            c = mul(&c, i);
        }
    }
    c
}

fn pow2523(i: &Gf) -> Gf {
    let mut c = *i;
    for a in (0..=250).rev() {
        c = sq(&c);
        if a != 1 {
            c = mul(&c, i);
        }
    }
    c
}

fn unpackneg(r: &mut [Gf; 4], p: &[u8; 32]) -> bool {
    // Reject non-canonical y encodings (>= p = 2^255 - 19).
    let mut y_bytes = *p;
    y_bytes[31] &= 0x7f;
    let t = GF0;
    let chk_y = unpack25519(&y_bytes);
    // Re-pack checks if >= p: if chk_y is canonical, pack25519(chk_y) == y_bytes.
    if pack25519(&add(&chk_y, &t)) != y_bytes {
        return false;
    }

    r[1] = chk_y;
    r[2] = GF1;
    let num = sub(&sq(&r[1]), &r[2]);
    let den = add(&mul(&sq(&r[1]), &D), &r[2]);
    let den2 = sq(&den);
    let den4 = sq(&den2);
    let den6 = mul(&den4, &den2);
    let num_den3 = mul(&num, &mul(&den2, &den));
    let mut x = mul(&num_den3, &pow2523(&mul(&num, &mul(&den6, &den))));
    let mut chk = mul(&sq(&x), &den);
    if neq25519(&chk, &num) {
        x = mul(&x, &I);
    }
    chk = mul(&sq(&x), &den);
    if neq25519(&chk, &num) {
        return false;
    }
    if !neq25519(&x, &GF0) && (p[31] >> 7) != 0 {
        return false;
    }
    if par25519(&x) == (p[31] >> 7) {
        x = sub(&GF0, &x);
    }
    r[0] = x;
    r[3] = mul(&r[0], &r[1]);
    true
}

fn point_add(p: &mut [Gf; 4], q: &[Gf; 4]) {
    let a = mul(&sub(&p[1], &p[0]), &sub(&q[1], &q[0]));
    let b = mul(&add(&p[1], &p[0]), &add(&q[1], &q[0]));
    let c = mul(&mul(&p[3], &q[3]), &D2);
    let d = mul(&add(&p[2], &p[2]), &q[2]);
    let e = sub(&b, &a);
    let f = sub(&d, &c);
    let g = add(&d, &c);
    let h = add(&b, &a);
    p[0] = mul(&e, &f);
    p[1] = mul(&g, &h);
    p[2] = mul(&f, &g);
    p[3] = mul(&e, &h);
}

fn cswap(p: &mut [Gf; 4], q: &mut [Gf; 4], b: i64) {
    for i in 0..4 {
        sel25519(&mut p[i], &mut q[i], b);
    }
}

fn scalarmult(q: &[Gf; 4], s: &[u8; 32]) -> [Gf; 4] {
    let mut p = [GF0, GF1, GF1, GF0];
    let mut base = *q;
    for i in (0..=255).rev() {
        let b = i64::from((s[i / 8] >> (i & 7)) & 1);
        cswap(&mut p, &mut base, b);
        point_add(&mut base, &p);
        let copy = p;
        point_add(&mut p, &copy);
        cswap(&mut p, &mut base, b);
    }
    p
}

fn scalarbase(s: &[u8; 32]) -> [Gf; 4] {
    let q = [X, Y, GF1, mul(&X, &Y)];
    scalarmult(&q, s)
}

fn pack_point(q: &[Gf; 4]) -> [u8; 32] {
    let zi = inv25519(&q[2]);
    let tx = mul(&q[0], &zi);
    let ty = mul(&q[1], &zi);
    let mut r = pack25519(&ty);
    r[31] ^= par25519(&tx) << 7;
    r
}

fn reduce_mod_l(h: &[u8; 64]) -> [u8; 32] {
    let mut x = [0i64; 64];
    for i in 0..64 {
        x[i] = i64::from(h[i]);
    }
    for i in (32..=63).rev() {
        let mut carry = 0i64;
        for j in (i - 32)..(i - 12) {
            x[j] += carry - 16 * x[i] * i64::from(L[j - (i - 32)]);
            carry = (x[j] + 128) >> 8;
            x[j] -= carry << 8;
        }
        x[i - 12] += carry;
        x[i] = 0;
    }
    let mut carry = 0i64;
    for j in 0..32 {
        x[j] += carry - (x[31] >> 4) * i64::from(L[j]);
        carry = x[j] >> 8;
        x[j] &= 255;
    }
    for j in 0..32 {
        x[j] -= carry * i64::from(L[j]);
    }
    for i in 0..31 {
        x[i + 1] += x[i] >> 8;
        x[i] &= 255;
    }
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = (x[i] & 0xff) as u8;
    }
    out
}

fn scalar_lt_l(s: &[u8; 32]) -> bool {
    for i in (0..32).rev() {
        if s[i] < L[i] {
            return true;
        }
        if s[i] > L[i] {
            return false;
        }
    }
    false
}

pub fn verify(public_key: &[u8; 32], message: &[u8], signature: &[u8; 64]) -> bool {
    let r_bytes: [u8; 32] = signature[..32].try_into().unwrap();
    let s_bytes: [u8; 32] = signature[32..].try_into().unwrap();
    if !scalar_lt_l(&s_bytes) {
        return false;
    }
    let mut neg_a = [GF0; 4];
    if !unpackneg(&mut neg_a, public_key) {
        return false;
    }
    // Verify R encodes a valid point too so cofactor malleability is constrained identically to RFC 8032.
    let mut check_r = [GF0; 4];
    if !unpackneg(&mut check_r, &r_bytes) {
        return false;
    }

    let mut hasher = Sha512::new();
    hasher.update(r_bytes);
    hasher.update(public_key);
    hasher.update(message);
    let digest: [u8; 64] = hasher.finalize().into();
    let h = reduce_mod_l(&digest);

    let mut p = scalarmult(&neg_a, &h);
    let q = scalarbase(&s_bytes);
    point_add(&mut p, &q);
    pack_point(&p) == r_bytes
}

pub fn decode_hex<const N: usize>(hex: &str) -> Result<[u8; N], String> {
    let trimmed = hex.trim();
    if trimmed.len() != N * 2 {
        return Err(format!("Expected {} hex characters, got {}", N * 2, trimmed.len()));
    }
    let bytes = trimmed.as_bytes();
    let mut out = [0u8; N];
    for i in 0..N {
        let hi = match bytes[2 * i] {
            b'0'..=b'9' => bytes[2 * i] - b'0',
            b'a'..=b'f' => bytes[2 * i] - b'a' + 10,
            b'A'..=b'F' => bytes[2 * i] - b'A' + 10,
            _ => return Err("Invalid hex character".to_owned()),
        };
        let lo = match bytes[2 * i + 1] {
            b'0'..=b'9' => bytes[2 * i + 1] - b'0',
            b'a'..=b'f' => bytes[2 * i + 1] - b'a' + 10,
            b'A'..=b'F' => bytes[2 * i + 1] - b'A' + 10,
            _ => return Err("Invalid hex character".to_owned()),
        };
        out[i] = (hi << 4) | lo;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_zero_x_with_nonzero_sign_bit() {
        // y = 1 and y = -1 both recover x = 0 (RFC 8032 section 5.1.3).
        for y in [
            decode_hex::<32>("0100000000000000000000000000000000000000000000000000000000000000").unwrap(),
            decode_hex::<32>("ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f").unwrap(),
        ] {
            assert!(unpackneg(&mut [GF0; 4], &y));
            let mut noncanonical = y;
            noncanonical[31] |= 0x80;
            assert!(!unpackneg(&mut [GF0; 4], &noncanonical));
        }
    }

    #[test]
    fn rfc8032_section_7_1_test_vectors() {
        // TEST 1 (empty message)
        let pk1 = decode_hex::<32>("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a").unwrap();
        let sig1 = decode_hex::<64>(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155\
             5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        ).unwrap();
        assert!(verify(&pk1, b"", &sig1));
        assert!(!verify(&pk1, b"x", &sig1));

        // TEST 2 (1-byte message 0x72)
        let pk2 = decode_hex::<32>("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c").unwrap();
        let sig2 = decode_hex::<64>(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da\
             085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
        ).unwrap();
        assert!(verify(&pk2, &[0x72], &sig2));

        // TEST 3 (2-byte message 0xaf82)
        let pk3 = decode_hex::<32>("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025").unwrap();
        let sig3 = decode_hex::<64>(
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac\
             18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
        ).unwrap();
        assert!(verify(&pk3, &[0xaf, 0x82], &sig3));

        // Reject non-canonical S >= L malleability
        let mut bad_s = sig1;
        bad_s[32..].copy_from_slice(&L);
        assert!(!verify(&pk1, b"", &bad_s));
    }
}
