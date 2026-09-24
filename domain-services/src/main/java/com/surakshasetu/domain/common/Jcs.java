package com.surakshasetu.domain.common;

import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import org.erdtman.jcs.JsonCanonicalizer;

/** RFC 8785 JSON Canonicalization Scheme: the only way this tier hashes JSON. */
public final class Jcs {

  private Jcs() {}

  public static byte[] canonicalize(String json) {
    try {
      return new JsonCanonicalizer(json).getEncodedUTF8();
    } catch (IOException e) {
      throw new IllegalArgumentException("Not valid JSON", e);
    }
  }

  public static String sha256Hex(String json) {
    try {
      return HexFormat.of()
          .formatHex(MessageDigest.getInstance("SHA-256").digest(canonicalize(json)));
    } catch (NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }
}
