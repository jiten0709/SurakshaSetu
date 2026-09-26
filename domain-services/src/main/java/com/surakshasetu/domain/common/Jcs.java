package com.surakshasetu.domain.common;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.text.Normalizer;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.erdtman.jcs.JsonCanonicalizer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * RFC 8785 JSON Canonicalization Scheme, and every hash this tier computes (the contract's
 * "Hashing" section). Nothing else in the tier hashes.
 */
public final class Jcs {

  private static final JsonMapper MAPPER = JsonMapper.builder().build();

  /** One entry of a disclosure set, as the set hash sees it. */
  public record SetItem(String disclosureId, String bodySha256) {}

  private Jcs() {}

  public static byte[] canonicalize(String json) {
    try {
      return new JsonCanonicalizer(json).getEncodedUTF8();
    } catch (IOException e) {
      throw new IllegalArgumentException("Not valid JSON", e);
    }
  }

  public static String sha256Hex(String json) {
    return sha256Hex(canonicalize(json));
  }

  public static String sha256Hex(byte[] bytes) {
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    } catch (NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }

  /**
   * SHA-256(JCS(model)) for a request the service built itself (a quote inside a ranking option or
   * an alternative), serialised as the contract's JSON.
   */
  public static String modelSha256(Object model) {
    return sha256Hex(MAPPER.writeValueAsString(model));
  }

  /** {@code body_sha256} = SHA-256(UTF-8(NFC(body))), for consent-notice and disclosure bodies. */
  public static String bodySha256(String body) {
    return sha256Hex(nfc(body).getBytes(StandardCharsets.UTF_8));
  }

  public static String nfc(String text) {
    return Normalizer.normalize(text, Normalizer.Form.NFC);
  }

  /**
   * Suitability {@code inputs_sha256} = SHA-256(JCS(needs)), over the {@code needs} object as sent
   * with its {@code slots_sha256} member removed. It must equal the orchestrator's {@code
   * slots_sha256} (I2); {@code content/testvectors/needs/} pins both tiers.
   */
  public static String needsSha256(JsonNode needs) {
    ObjectNode preimage = (ObjectNode) needs.deepCopy();
    preimage.remove("slots_sha256");
    return sha256Hex(MAPPER.writeValueAsString(preimage));
  }

  /**
   * {@code set_sha256} = SHA-256(JCS({registry_version, uin, channel, language, items:
   * [{disclosure_id, body_sha256}]})), items in set order.
   */
  public static String setSha256(
      String registryVersion, String uin, String channel, String language, List<SetItem> items) {
    Map<String, Object> set = new LinkedHashMap<>();
    set.put("registry_version", registryVersion);
    set.put("uin", uin);
    set.put("channel", channel);
    set.put("language", language);
    set.put(
        "items",
        items.stream()
            .map(i -> Map.of("disclosure_id", i.disclosureId(), "body_sha256", i.bodySha256()))
            .toList());
    return sha256Hex(MAPPER.writeValueAsString(set));
  }
}
