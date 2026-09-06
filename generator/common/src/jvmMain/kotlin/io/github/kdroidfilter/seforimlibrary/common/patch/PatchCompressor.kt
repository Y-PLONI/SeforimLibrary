package io.github.kdroidfilter.seforimlibrary.common.patch

import com.github.luben.zstd.EndDirective
import com.github.luben.zstd.ZstdCompressCtx
import com.github.luben.zstd.ZstdOutputStream
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.security.MessageDigest

/**
 * Compresses a freshly-produced `patch.db` with zstd and returns the
 * metadata both the manifest writer and the client need for verification.
 *
 * Output sits next to the input (`<patch>.zst`). Caller decides whether
 * to keep or delete the original `.db`.
 *
 * The frame header **must** carry the decompressed content size. The Otzaria
 * client decompresses through the `zstandard` FFI plugin, which allocates the
 * destination buffer from `ZSTD_getFrameContentSize`; when the size is
 * missing it falls back to `compressedSize * 20` in a single contiguous
 * allocation. A ~560 MB patch compressed by the old `ZstdOutputStream` path
 * (which never pledges a size) therefore asked for ~11 GB and failed with
 * `Could not allocate 11694234840 bytes`. Streaming via [ZstdCompressCtx] with
 * [ZstdCompressCtx.setPledgedSrcSize] keeps memory bounded here and writes the
 * exact size into the header.
 */
object PatchCompressor {

    data class Result(
        /** Path of the produced `<patch>.zst`. */
        val compressedFile: Path,
        /** sha256 of the `.zst` (= what the client downloads). */
        val compressedSha256: String,
        /** Size in bytes of the `.zst`. */
        val compressedSize: Long,
        /** sha256 of the original uncompressed patch.db (post-decompress verification). */
        val uncompressedSha256: String,
        /** Size in bytes of the uncompressed patch.db. */
        val uncompressedSize: Long,
    )

    /** 1 MiB input chunks; output buffer sized per zstd's recommendation. */
    private const val IN_CHUNK = 1 shl 20

    /**
     * Compresses [patchDb] to `<patchDb>.zst` at level [level].
     *
     * Default level 22 (ultra) matches the full bundle's compression
     * setting in `PackageArtifacts.kt` for end-to-end consistency.
     * Trade-off vs L19 in our measurements: a few percent smaller output
     * for ~5-10× CPU cost. Use a lower level (e.g. 19, 15) when CI
     * wall-time matters more than the marginal size win.
     */
    fun compress(patchDb: Path, level: Int = 22, workers: Int = Runtime.getRuntime().availableProcessors()): Result {
        require(Files.isRegularFile(patchDb)) { "patch.db not found: $patchDb" }
        val target = patchDb.resolveSibling("${patchDb.fileName}.zst")
        val uncompressedSha = MessageDigest.getInstance("SHA-256")
        val uncompressedSize = Files.size(patchDb)

        ZstdCompressCtx().use { ctx ->
            ctx.setLevel(level)
            ctx.setChecksum(true)
            // Enable multithreaded compression when workers > 1.
            if (workers > 1) ctx.setWorkers(workers)
            // Written into the frame header -> the client can size its buffer exactly.
            ctx.setPledgedSrcSize(uncompressedSize)

            val inBuf = ByteBuffer.allocateDirect(IN_CHUNK)
            val outBuf = ByteBuffer.allocateDirect(ZstdOutputStream.recommendedCOutSize().toInt())
            val hashView = ByteArray(IN_CHUNK)

            FileChannel.open(patchDb, StandardOpenOption.READ).use { input ->
                FileChannel.open(
                    target,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.TRUNCATE_EXISTING,
                    StandardOpenOption.WRITE,
                ).use { output ->
                    var remaining = uncompressedSize
                    while (true) {
                        inBuf.clear()
                        val n = input.read(inBuf)
                        if (n <= 0) break
                        remaining -= n
                        inBuf.flip()
                        inBuf.duplicate().get(hashView, 0, n)
                        uncompressedSha.update(hashView, 0, n)

                        // END on the last chunk finalises the frame in the same call,
                        // so the pledged size is honoured exactly.
                        val directive = if (remaining == 0L) EndDirective.END else EndDirective.CONTINUE
                        drain(ctx, inBuf, outBuf, output, directive)
                    }
                    if (uncompressedSize == 0L) {
                        // Empty patch: still emit a well-formed frame with size 0.
                        inBuf.clear().flip()
                        drain(ctx, inBuf, outBuf, output, EndDirective.END)
                    }
                }
            }
        }

        check(Files.size(target) > 0) { "zstd produced an empty file for $patchDb" }

        val compressedSha = sha256(target)
        return Result(
            compressedFile = target,
            compressedSha256 = hex(compressedSha),
            compressedSize = Files.size(target),
            uncompressedSha256 = hex(uncompressedSha.digest()),
            uncompressedSize = uncompressedSize,
        )
    }

    /**
     * Feeds [inBuf] to [ctx] until it is fully consumed, writing every produced
     * block to [output]. With [EndDirective.END] it also loops until zstd reports
     * the frame is complete (`compressDirectByteBufferStream` returns `true`).
     */
    private fun drain(
        ctx: ZstdCompressCtx,
        inBuf: ByteBuffer,
        outBuf: ByteBuffer,
        output: FileChannel,
        directive: EndDirective,
    ) {
        while (true) {
            outBuf.clear()
            val done = ctx.compressDirectByteBufferStream(outBuf, inBuf, directive)
            outBuf.flip()
            while (outBuf.hasRemaining()) output.write(outBuf)
            if (directive == EndDirective.END) {
                if (done) return
            } else if (!inBuf.hasRemaining()) {
                return
            }
        }
    }

    private fun sha256(path: Path): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        Files.newInputStream(path).use { stream ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val n = stream.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest()
    }

    private fun hex(bytes: ByteArray): String =
        bytes.joinToString("") { b -> "%02x".format(b) }

    /** Replace [source] with [destination] atomically. Used after a successful copy. */
    fun replaceAtomic(source: Path, destination: Path) {
        Files.move(source, destination, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    }
}
