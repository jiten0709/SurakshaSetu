package com.surakshasetu.domain.disclosure;

import static com.surakshasetu.domain.common.Problems.notFound;
import static com.surakshasetu.domain.common.Problems.problem;

import com.surakshasetu.domain.catalog.CatalogRepository;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import com.surakshasetu.domain.contract.DisclosureApi;
import com.surakshasetu.domain.contract.model.Disclosure;
import com.surakshasetu.domain.contract.model.DisclosureItem;
import com.surakshasetu.domain.contract.model.DisclosureSet;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.RestController;

/**
 * The Disclosure Registry. Every read recomputes the body and set hashes and refuses to serve a
 * mismatch (I4). A disclosure is keyed by (disclosure_id, language) since V8 (OD-1).
 */
@RestController
class DisclosureController implements DisclosureApi {

  private static final Logger log = LoggerFactory.getLogger(DisclosureController.class);
  private static final HexFormat HEX = HexFormat.of();
  private static final String EFFECTIVE =
      "d.effective_from <= :on AND (d.effective_to IS NULL OR :on < d.effective_to)";

  private final JdbcClient jdbc;
  private final CatalogRepository catalog;

  DisclosureController(JdbcClient jdbc, CatalogRepository catalog) {
    this.jdbc = jdbc;
    this.catalog = catalog;
  }

  private record Member(
      String disclosureId, @Nullable String body, byte @Nullable [] bodySha256, boolean isDummy) {}

  private record Candidate(String registryVersion, byte[] setSha256) {}

  /**
   * The newest registry version for (uin, channel, language) whose every member is in force on
   * as_of. disclosure_set carries no dates of its own, so its members' dates decide.
   */
  @Override
  public ResponseEntity<DisclosureSet> getDisclosureSet(
      String uin, String channel, String language, @Nullable OffsetDateTime asOf) {
    if (!catalog.exists(uin)) {
      throw notFound("no product with this UIN");
    }
    LocalDate on = BusinessDates.on(asOf);
    List<Candidate> candidates =
        jdbc.sql(
                """
                SELECT registry_version, set_sha256 FROM catalog.disclosure_set
                WHERE uin = ? AND channel = ? AND language = ?
                ORDER BY string_to_array(registry_version, '.')::int[] DESC
                """)
            .params(uin, channel, language)
            .query((rs, n) -> new Candidate(rs.getString(1), rs.getBytes(2)))
            .list();
    for (Candidate candidate : candidates) {
      List<Member> members = members(uin, channel, language, candidate.registryVersion(), on);
      if (members.stream().allMatch(m -> m.body() != null)) {
        return ResponseEntity.ok(verified(uin, channel, language, candidate, members));
      }
    }
    throw notFound("no disclosure set in force for this product, channel and language");
  }

  @Override
  public ResponseEntity<Disclosure> getDisclosure(
      String disclosureId, String language, @Nullable OffsetDateTime asOf) {
    Disclosure disclosure =
        jdbc.sql(
                "SELECT d.body, d.body_sha256, d.is_dummy FROM catalog.disclosure d"
                    + " WHERE d.disclosure_id = :id AND d.language = :language AND "
                    + EFFECTIVE)
            .param("id", disclosureId)
            .param("language", language)
            .param("on", BusinessDates.on(asOf))
            .query(
                (rs, n) ->
                    new Disclosure(
                        disclosureId,
                        language,
                        rs.getString("body"),
                        HEX.formatHex(rs.getBytes("body_sha256")),
                        rs.getBoolean("is_dummy")))
            .optional()
            .orElseThrow(() -> notFound("no such disclosure in force for the language"));
    if (!Jcs.bodySha256(disclosure.getBody()).equals(disclosure.getBodySha256())) {
      throw integrity("disclosure " + disclosureId + " " + language);
    }
    return ResponseEntity.ok(disclosure);
  }

  /** The set's members in order; body is null for a member not in force on {@code on}. */
  private List<Member> members(
      String uin, String channel, String language, String registryVersion, LocalDate on) {
    return jdbc.sql(
            """
            SELECT m.id, d.body, d.body_sha256, d.is_dummy
            FROM catalog.disclosure_set s
            CROSS JOIN LATERAL unnest(s.disclosure_ids) WITH ORDINALITY AS m(id, ord)
            LEFT JOIN catalog.disclosure d ON d.disclosure_id = m.id AND d.language = s.language
              AND %s
            WHERE s.uin = :uin AND s.channel = :channel AND s.language = :language
              AND s.registry_version = :version
            ORDER BY m.ord
            """
                .formatted(EFFECTIVE))
        .param("uin", uin)
        .param("channel", channel)
        .param("language", language)
        .param("version", registryVersion)
        .param("on", on)
        .query(
            (rs, n) ->
                new Member(rs.getString(1), rs.getString(2), rs.getBytes(3), rs.getBoolean(4)))
        .list();
  }

  private DisclosureSet verified(
      String uin, String channel, String language, Candidate candidate, List<Member> members) {
    String where =
        uin + "/" + channel + "/" + language + " registry " + candidate.registryVersion();
    List<DisclosureItem> items =
        members.stream()
            .map(m -> new DisclosureItem(m.disclosureId(), m.body(), HEX.formatHex(m.bodySha256())))
            .toList();
    for (DisclosureItem item : items) {
      if (!Jcs.bodySha256(item.getBody()).equals(item.getBodySha256())) {
        throw integrity("set " + where + ", item " + item.getDisclosureId());
      }
    }
    String setSha256 =
        Jcs.setSha256(
            candidate.registryVersion(),
            uin,
            channel,
            language,
            items.stream()
                .map(i -> new Jcs.SetItem(i.getDisclosureId(), i.getBodySha256()))
                .toList());
    if (!setSha256.equals(HEX.formatHex(candidate.setSha256()))) {
      throw integrity("set " + where);
    }
    return new DisclosureSet(
        uin,
        channel,
        language,
        candidate.registryVersion(),
        items,
        setSha256,
        members.stream().anyMatch(Member::isDummy));
  }

  /** Never served: the caller gets a 500, and an ERROR log names the set or item. */
  private static RuntimeException integrity(String what) {
    log.error("REGISTRY_INTEGRITY: {} fails its hash check; not served", what);
    return problem(
        HttpStatus.INTERNAL_SERVER_ERROR,
        "REGISTRY_INTEGRITY",
        "a stored disclosure failed its hash check and was not served");
  }
}
