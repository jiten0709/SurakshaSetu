package com.surakshasetu.domain.consent;

import static com.surakshasetu.domain.common.Problems.notFound;
import static com.surakshasetu.domain.common.Problems.problem;

import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Uuid7;
import com.surakshasetu.domain.contract.ConsentApi;
import com.surakshasetu.domain.contract.model.ConsentMethod;
import com.surakshasetu.domain.contract.model.ConsentNotice;
import com.surakshasetu.domain.contract.model.ConsentRecord;
import com.surakshasetu.domain.contract.model.ConsentRecordCreate;
import com.surakshasetu.domain.contract.model.ConsentWithdrawal;
import com.surakshasetu.domain.contract.model.PurposeGrant;
import com.surakshasetu.domain.contract.model.PurposeId;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.RestController;

/**
 * The Consent Service, the only writer of consent (as domain_rw). A record is immutable except for
 * its withdrawal; purposes are an append-only history whose newest row per purpose is current. Logs
 * never carry a consent id, subject, session, idempotency key or request value.
 */
@RestController
class ConsentController implements ConsentApi {

  private static final Logger log = LoggerFactory.getLogger(ConsentController.class);
  private static final HexFormat HEX = HexFormat.of();
  // API purpose ids <-> consent.purpose_grant.purpose.
  private static final Map<PurposeId, String> DB_PURPOSE =
      Map.of(
          PurposeId.P1_NEEDS_RECO, "P1",
          PurposeId.P2_ADVISOR_CONTACT, "P2",
          PurposeId.P3_MARKETING, "P3");
  private static final Map<String, PurposeId> API_PURPOSE =
      DB_PURPOSE.entrySet().stream()
          .collect(Collectors.toMap(Map.Entry::getValue, Map.Entry::getKey));

  private final JdbcClient jdbc;
  private final TransactionTemplate tx;
  private final Duration ttl;

  ConsentController(
      JdbcClient jdbc,
      TransactionTemplate tx,
      @Value("${surakshasetu.consent.ttl-days}") long ttlDays) {
    this.jdbc = jdbc;
    this.tx = tx;
    this.ttl = Duration.ofDays(ttlDays);
  }

  @Override
  public ResponseEntity<ConsentRecord> createConsentRecord(
      String idempotencyKey, ConsentRecordCreate request) {
    Optional<UUID> existing = byIdempotencyKey(idempotencyKey);
    if (existing.isPresent()) {
      return replay(existing.get(), request);
    }
    Map<String, Boolean> purposes = validPurposes(request.getPurposes());
    checkNotice(request);

    UUID consentId = Uuid7.next();
    OffsetDateTime now = BusinessDates.now();
    try {
      tx.executeWithoutResult(
          status -> {
            jdbc.sql(
                    """
                    INSERT INTO consent.record (consent_id, subject_ref, session_id, notice_version,
                      method, is_adult_declared, granted_at, idempotency_key, notice_sha256,
                      ai_disclosure_version, language)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """)
                .params(
                    consentId,
                    request.getSubjectRef(),
                    request.getSessionId(),
                    request.getNoticeVersion(),
                    request.getMethod().getValue(),
                    request.getAge18PlusDeclared(),
                    now,
                    idempotencyKey,
                    HEX.parseHex(request.getNoticeSha256()),
                    request.getAiDisclosureVersion(),
                    request.getLanguage())
                .update();
            purposes.forEach((purpose, granted) -> appendPurpose(consentId, purpose, granted, now));
          });
    } catch (DuplicateKeyException raced) {
      // A concurrent request with the same key won; answer as its replay.
      return replay(byIdempotencyKey(idempotencyKey).orElseThrow(), request);
    }
    log.info(
        "consent recorded: {} purposes, method {}",
        purposes.size(),
        request.getMethod().getValue());
    return ResponseEntity.status(HttpStatus.CREATED).body(record(consentId, now));
  }

  @Override
  public ResponseEntity<ConsentRecord> getConsentRecord(
      UUID consentId, @Nullable OffsetDateTime asOf) {
    return ResponseEntity.ok(record(consentId, BusinessDates.orNow(asOf)));
  }

  @Override
  public ResponseEntity<ConsentRecord> changeConsentPurpose(UUID consentId, PurposeGrant grant) {
    OffsetDateTime now = BusinessDates.now();
    tx.executeWithoutResult(
        status -> {
          // Row lock (domain_rw's UPDATE on withdrawn_at allows it) so a withdrawal can't slip in.
          Optional<Boolean> withdrawn =
              jdbc.sql(
                      "SELECT withdrawn_at IS NOT NULL FROM consent.record"
                          + " WHERE consent_id = ? FOR UPDATE")
                  .param(consentId)
                  .query(Boolean.class)
                  .optional();
          if (withdrawn.isEmpty()) {
            throw notFound("consent record not found");
          }
          if (withdrawn.get()) {
            throw problem(
                HttpStatus.CONFLICT,
                "CONSENT_WITHDRAWN",
                "consent was withdrawn; capture a new consent record");
          }
          appendPurpose(consentId, DB_PURPOSE.get(grant.getPurposeId()), grant.getGranted(), now);
        });
    log.debug("purpose {} set to {}", grant.getPurposeId().getValue(), grant.getGranted());
    return ResponseEntity.ok(record(consentId, now));
  }

  @Override
  public ResponseEntity<ConsentRecord> withdrawConsent(
      UUID consentId, ConsentWithdrawal withdrawal) {
    OffsetDateTime now = BusinessDates.now();
    boolean withdrawnNow =
        Boolean.TRUE.equals(
            tx.execute(
                status -> {
                  int updated =
                      jdbc.sql(
                              "UPDATE consent.record SET withdrawn_at = ?"
                                  + " WHERE consent_id = ? AND withdrawn_at IS NULL")
                          .params(now, consentId)
                          .update();
                  if (updated == 1) {
                    DB_PURPOSE.values().forEach(p -> appendPurpose(consentId, p, false, now));
                  }
                  return updated == 1;
                }));
    if (withdrawnNow) {
      log.info("consent withdrawn");
    } else {
      log.debug("withdrawal replayed");
    }
    return ResponseEntity.ok(record(consentId, now)); // 404 when the record does not exist
  }

  @Override
  public ResponseEntity<ConsentNotice> getCurrentConsentNotice(String language) {
    return ResponseEntity.ok(
        notice(
                "WHERE language = ? AND effective_from <= ?"
                    + " ORDER BY effective_from DESC, notice_version DESC LIMIT 1",
                language,
                BusinessDates.on(null))
            .orElseThrow(() -> notFound("no consent notice in force for the language")));
  }

  @Override
  public ResponseEntity<ConsentNotice> getConsentNotice(String noticeVersion) {
    return ResponseEntity.ok(
        notice("WHERE notice_version = ?", noticeVersion)
            .orElseThrow(() -> notFound("unknown notice version")));
  }

  private Optional<ConsentNotice> notice(String where, Object... params) {
    return jdbc.sql(
            "SELECT notice_version, language, body, body_sha256, is_dummy"
                + " FROM consent.notice_version "
                + where)
        .params(params)
        .query(
            (rs, n) ->
                new ConsentNotice(
                    rs.getString("notice_version"),
                    rs.getString("language"),
                    rs.getString("body"),
                    HEX.formatHex(rs.getBytes("body_sha256")),
                    rs.getBoolean("is_dummy")))
        .optional();
  }

  /** The request's purposes by DB code; P1 must be present and granted. */
  private static Map<String, Boolean> validPurposes(List<PurposeGrant> grants) {
    Map<String, Boolean> purposes = new LinkedHashMap<>();
    for (PurposeGrant grant : grants) {
      if (purposes.put(DB_PURPOSE.get(grant.getPurposeId()), grant.getGranted()) != null) {
        throw problem(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "a purpose appears twice");
      }
    }
    if (!Boolean.TRUE.equals(purposes.get("P1"))) {
      throw problem(
          HttpStatus.UNPROCESSABLE_CONTENT, "P1_REQUIRED", "P1 must be granted to record consent");
    }
    return purposes;
  }

  /** The notice exists, is the one in force for its language today, and matches the hash. */
  private void checkNotice(ConsentRecordCreate request) {
    Optional<String> current =
        jdbc.sql(
                """
                SELECT notice_version FROM consent.notice_version
                WHERE language = ? AND effective_from <= ?
                ORDER BY effective_from DESC, notice_version DESC LIMIT 1
                """)
            .params(request.getLanguage(), BusinessDates.on(null))
            .query(String.class)
            .optional();
    if (!current.equals(Optional.of(request.getNoticeVersion()))) {
      throw noticeMismatch("the notice version is not the one in force for the language");
    }
    byte[] sha =
        jdbc.sql("SELECT body_sha256 FROM consent.notice_version WHERE notice_version = ?")
            .param(request.getNoticeVersion())
            .query(byte[].class)
            .single();
    if (!HEX.formatHex(sha).equals(request.getNoticeSha256())) {
      throw noticeMismatch("notice_sha256 does not match the notice version");
    }
  }

  private static RuntimeException noticeMismatch(String detail) {
    return problem(HttpStatus.UNPROCESSABLE_CONTENT, "NOTICE_MISMATCH", detail);
  }

  private void appendPurpose(UUID consentId, String purpose, boolean granted, OffsetDateTime at) {
    jdbc.sql(
            "INSERT INTO consent.purpose_grant (consent_id, purpose, granted, changed_at)"
                + " VALUES (?, ?, ?, ?)")
        .params(consentId, purpose, granted, at)
        .update();
  }

  private Optional<UUID> byIdempotencyKey(String key) {
    return jdbc.sql("SELECT consent_id FROM consent.record WHERE idempotency_key = ?")
        .param(key)
        .query(UUID.class)
        .optional();
  }

  /** Same key and same body: the original record, 200. Same key, other body: 409. */
  private ResponseEntity<ConsentRecord> replay(UUID consentId, ConsentRecordCreate request) {
    Stored stored = stored(consentId);
    Map<String, Boolean> initialPurposes =
        jdbc
            .sql(
                """
                SELECT g.purpose, g.granted FROM consent.purpose_grant g
                JOIN consent.record r USING (consent_id)
                WHERE g.consent_id = ? AND g.changed_at = r.granted_at
                """)
            .param(consentId)
            .query((rs, n) -> Map.entry(rs.getString(1), rs.getBoolean(2)))
            .list()
            .stream()
            .collect(Collectors.toMap(Map.Entry::getKey, Map.Entry::getValue));
    Set<Map.Entry<String, Boolean>> requested =
        request.getPurposes().stream()
            .map(g -> Map.entry(DB_PURPOSE.get(g.getPurposeId()), g.getGranted()))
            .collect(Collectors.toSet());
    boolean same =
        stored.sessionId().equals(request.getSessionId())
            && stored.subjectRef().equals(request.getSubjectRef())
            && stored.noticeVersion().equals(request.getNoticeVersion())
            && stored.noticeSha256().equals(request.getNoticeSha256())
            && stored.language().equals(request.getLanguage())
            && stored.aiDisclosureVersion().equals(request.getAiDisclosureVersion())
            && stored.adult() == request.getAge18PlusDeclared()
            && stored.method().equals(request.getMethod().getValue())
            && initialPurposes.entrySet().equals(requested)
            && requested.size() == request.getPurposes().size();
    if (!same) {
      throw problem(
          HttpStatus.CONFLICT,
          "IDEMPOTENCY_KEY_REUSED",
          "the Idempotency-Key was used with a different body");
    }
    log.debug("consent create replayed");
    return ResponseEntity.ok(record(consentId, BusinessDates.now()));
  }

  private record Stored(
      UUID sessionId,
      UUID subjectRef,
      String noticeVersion,
      String noticeSha256,
      String language,
      String aiDisclosureVersion,
      boolean adult,
      String method,
      OffsetDateTime grantedAt,
      @Nullable OffsetDateTime withdrawnAt) {}

  private Stored stored(UUID consentId) {
    return jdbc.sql(
            """
            SELECT session_id, subject_ref, notice_version, notice_sha256, language,
              ai_disclosure_version, is_adult_declared, method, granted_at, withdrawn_at
            FROM consent.record WHERE consent_id = ?
            """)
        .param(consentId)
        .query(
            (rs, n) ->
                new Stored(
                    rs.getObject("session_id", UUID.class),
                    rs.getObject("subject_ref", UUID.class),
                    rs.getString("notice_version"),
                    HEX.formatHex(rs.getBytes("notice_sha256")),
                    rs.getString("language"),
                    rs.getString("ai_disclosure_version"),
                    rs.getBoolean("is_adult_declared"),
                    rs.getString("method"),
                    rs.getObject("granted_at", OffsetDateTime.class),
                    rs.getObject("withdrawn_at", OffsetDateTime.class)))
        .optional()
        .orElseThrow(() -> notFound("consent record not found"));
  }

  /**
   * The record with valid_p1 evaluated at {@code asOf}. as_of moves the TTL and the notice check
   * only: a withdrawal or a revoked P1 holds whatever instant is asked about.
   */
  private ConsentRecord record(UUID consentId, OffsetDateTime asOf) {
    Stored stored = stored(consentId);
    // Newest row per purpose, in P1, P2, P3 order.
    List<PurposeGrant> current =
        jdbc.sql(
                """
                SELECT DISTINCT ON (purpose) purpose, granted FROM consent.purpose_grant
                WHERE consent_id = ? ORDER BY purpose, changed_at DESC
                """)
            .param(consentId)
            .query((rs, n) -> new PurposeGrant(API_PURPOSE.get(rs.getString(1)), rs.getBoolean(2)))
            .list();
    boolean p1 =
        current.stream()
            .anyMatch(g -> g.getPurposeId() == PurposeId.P1_NEEDS_RECO && g.getGranted());
    boolean superseded =
        jdbc.sql(
                """
                SELECT EXISTS (SELECT 1 FROM consent.notice_version n
                  JOIN consent.notice_version pinned ON pinned.notice_version = ?
                  WHERE n.language = pinned.language AND n.effective_from > pinned.effective_from
                    AND n.effective_from <= ?)
                """)
            .params(stored.noticeVersion(), BusinessDates.on(asOf))
            .query(Boolean.class)
            .single();

    List<String> reasons = new ArrayList<>();
    if (!p1) {
      reasons.add("P1_NOT_GRANTED");
    }
    if (stored.withdrawnAt() != null) {
      reasons.add("WITHDRAWN");
    }
    if (!stored.adult()) {
      reasons.add("AGE_NOT_DECLARED");
    }
    if (superseded) {
      reasons.add("NOTICE_SUPERSEDED");
    }
    if (Duration.between(stored.grantedAt(), asOf).compareTo(ttl) >= 0) {
      reasons.add("CONSENT_EXPIRED");
    }

    return new ConsentRecord(
            consentId,
            stored.noticeVersion(),
            stored.noticeSha256(),
            stored.language(),
            stored.aiDisclosureVersion(),
            current,
            stored.adult(),
            ConsentMethod.fromValue(stored.method()),
            stored.grantedAt(),
            reasons.isEmpty(),
            reasons)
        .withdrawnAt(stored.withdrawnAt());
  }
}
