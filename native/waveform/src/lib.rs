//! Fused PCM decoding and peak/RMS accumulation. No Python or third-party dependencies.
use std::slice;

#[no_mangle]
pub extern "C" fn sfm_waveform_abi() -> u32 {
    1
}

fn clean(value: f32) -> f32 {
    if value.is_nan() {
        0.0
    } else if value == f32::INFINITY {
        1.0
    } else if value == f32::NEG_INFINITY {
        -1.0
    } else {
        value
    }
}

fn reduce(
    raw: &[u8],
    width: usize,
    channels: usize,
    cuts: &[u64],
    peaks: &mut [f32],
    sums: &mut [f64],
    decode: impl Fn(&[u8]) -> f32,
) {
    let buckets = cuts.len() - 1;
    let align = width * channels;
    let mut peak = vec![0.0_f32; channels];
    let mut sum = vec![0.0_f64; channels];
    for (bucket, edge) in cuts.windows(2).enumerate() {
        peak.fill(0.0);
        sum.fill(0.0);
        for frame in raw[edge[0] as usize * align..edge[1] as usize * align].chunks_exact(align) {
            for (channel, sample) in frame.chunks_exact(width).enumerate() {
                let value = decode(sample).abs();
                peak[channel] = peak[channel].max(value);
                sum[channel] += f64::from(value) * f64::from(value);
            }
        }
        for channel in 0..channels {
            peaks[channel * buckets + bucket] = peak[channel];
            sums[channel * buckets + bucket] = sum[channel];
        }
    }
}

/// Returns 0 on success, 1 for unsupported formats or invalid dimensions/cuts.
///
/// # Safety
/// The caller supplies readable `raw_len` bytes and `buckets + 1` aligned u64
/// cuts, plus nonoverlapping writable arrays of `channels * buckets` f32/f64
/// elements. All buffers must remain valid for the duration of this call.
#[no_mangle]
pub unsafe extern "C" fn sfm_waveform_reduce(
    raw: *const u8,
    raw_len: usize,
    bits: u32,
    channels: usize,
    is_float: u32,
    cuts: *const u64,
    buckets: usize,
    peaks: *mut f32,
    sums: *mut f64,
) -> i32 {
    let width = (bits / 8) as usize;
    if channels == 0
        || buckets == 0
        || width == 0
        || raw.is_null()
        || cuts.is_null()
        || peaks.is_null()
        || sums.is_null()
        || channels.checked_mul(width).is_none()
        || buckets.checked_add(1).is_none()
        || channels.checked_mul(buckets).is_none()
    {
        return 1;
    }
    if !matches!((is_float, bits), (0, 8 | 16 | 24 | 32) | (1, 32 | 64)) {
        return 1;
    }
    let align = channels * width;
    let cuts = slice::from_raw_parts(cuts, buckets + 1);
    let frames = raw_len / align;
    if cuts[0] != 0 || cuts[buckets] != frames as u64 || cuts.windows(2).any(|w| w[0] > w[1]) {
        return 1;
    }
    let raw = slice::from_raw_parts(raw, raw_len);
    let peaks = slice::from_raw_parts_mut(peaks, channels * buckets);
    let sums = slice::from_raw_parts_mut(sums, channels * buckets);
    match (is_float, bits) {
        (0, 8) => reduce(raw, width, channels, cuts, peaks, sums, |s| {
            (f32::from(s[0]) - 128.0) / 128.0
        }),
        (0, 16) => reduce(raw, width, channels, cuts, peaks, sums, |s| {
            f32::from(i16::from_le_bytes([s[0], s[1]])) / 32768.0
        }),
        (0, 24) => reduce(raw, width, channels, cuts, peaks, sums, |s| {
            i32::from_le_bytes([0, s[0], s[1], s[2]]) as f32 / 2147483648.0
        }),
        (0, 32) => reduce(raw, width, channels, cuts, peaks, sums, |s| {
            i32::from_le_bytes(s.try_into().unwrap()) as f32 / 2147483648.0
        }),
        (1, 32) => reduce(raw, width, channels, cuts, peaks, sums, |s| {
            clean(f32::from_le_bytes(s.try_into().unwrap()))
        }),
        (1, 64) => reduce(raw, width, channels, cuts, peaks, sums, |s| {
            clean(f64::from_le_bytes(s.try_into().unwrap()) as f32)
        }),
        _ => return 1,
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stereo_buckets_and_signed_pcm() {
        let samples = [i16::MIN, 0, i16::MAX, 16384, 0, -16384];
        let raw: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        let mut peaks = [0.0; 4];
        let mut sums = [0.0; 4];
        let cuts = [0, 2, 3];
        assert_eq!(
            unsafe {
                sfm_waveform_reduce(
                    raw.as_ptr(),
                    raw.len(),
                    16,
                    2,
                    0,
                    cuts.as_ptr(),
                    2,
                    peaks.as_mut_ptr(),
                    sums.as_mut_ptr(),
                )
            },
            0
        );
        assert_eq!(peaks, [1.0, 0.0, 0.5, 0.5]);
        assert_eq!(sums[2..], [0.25, 0.25]);
    }

    #[test]
    fn invalid_cuts_are_rejected() {
        let raw = [0_u8; 8];
        let mut peaks = [0.0; 2];
        let mut sums = [0.0; 2];
        for cuts in [[0, 5, 4], [1, 2, 4], [0, 2, 5]] {
            assert_eq!(
                unsafe {
                    sfm_waveform_reduce(
                        raw.as_ptr(),
                        raw.len(),
                        16,
                        1,
                        0,
                        cuts.as_ptr(),
                        2,
                        peaks.as_mut_ptr(),
                        sums.as_mut_ptr(),
                    )
                },
                1
            );
        }
    }
}
