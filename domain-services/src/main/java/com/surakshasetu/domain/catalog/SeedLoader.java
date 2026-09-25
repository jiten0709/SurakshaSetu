package com.surakshasetu.domain.catalog;

import com.surakshasetu.domain.common.CatalogLoaderDb;
import com.surakshasetu.domain.common.Jcs;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.CommandLineRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Profile;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.dataformat.yaml.YAMLMapper;

/**
 * Loads content/seed into the catalog (as catalog_loader) and the consent notices (as domain_rw),
 * computing every body, set and document hash. Idempotent: an unchanged re-run changes nothing.
 * Legally significant rows are immutable: a changed disclosure body, disclosure set, notice or
 * document under an existing key fails the whole load, since acknowledgments and set hashes already
 * point at the old text. {@code make seed-catalog} runs it under profile catalog-sync.
 */
@Component
public class SeedLoader {

  private static final Logger log = LoggerFactory.getLogger(SeedLoader.class);
  private static final HexFormat HEX = HexFormat.of();
  private static final Pattern REGISTRY_VERSION = Pattern.compile("^\\d{4}\\.\\d{2}\\.\\d+$");
  private static final YAMLMapper YAML =
      YAMLMapper.builder()
          .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
          .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
          // Every row key must be written, nulls included: a forgotten is_dummy is an error,
          // never a silent false.
          .enable(DeserializationFeature.FAIL_ON_MISSING_CREATOR_PROPERTIES)
          .build();
  private static final JsonMapper JSON = JsonMapper.builder().build();

  record ProductSeed(
      String uin,
      String name,
      String category,
      int entryAgeMin,
      int entryAgeMax,
      int maturityAgeMax,
      BigDecimal saMinInr,
      @Nullable BigDecimal saMaxInr,
      int termYearsMin,
      int termYearsMax,
      List<String> pptOptions,
      List<String> benefitPaymentOptions,
      List<String> riderUins,
      LocalDate effectiveFrom,
      Map<String, Object> quoteDefaults,
      boolean launchEnabled,
      boolean isDummy) {}

  record RiderSeed(
      String uin,
      String name,
      List<String> attachesTo,
      @Nullable BigDecimal saMaxInr,
      boolean isDummy) {}

  /** path is relative to the seed directory; the stored uri is content/seed/{path}. */
  record DocumentSeed(
      String uin, String kind, String version, String language, String path, boolean isDummy) {}

  /** One disclosure_id with its body per language: one catalog.disclosure row each. */
  record DisclosureSeed(
      String disclosureId,
      String scope,
      @Nullable String scopeKey,
      String approvedBy,
      LocalDate effectiveFrom,
      @Nullable LocalDate effectiveTo,
      boolean isDummy,
      Map<String, String> bodies) {}

  /** One ordered set, stored once per channel and language. */
  record SetSeed(
      String uin,
      String registryVersion,
      List<String> channels,
      List<String> languages,
      List<String> disclosureIds) {}

  record PincodeSeed(
      String pincode, String district, String state, boolean serviceable, boolean isDummy) {}

  record OccupationSeed(String code, String label, int riskClass, boolean isDummy) {}

  record NoticeSeed(
      String noticeVersion,
      String language,
      String body,
      String approvedBy,
      LocalDate effectiveFrom,
      boolean isDummy) {}

  /** A seed file holds any subset of these top-level keys; files merge by key. */
  static final class SeedFile {
    public List<ProductSeed> products = List.of();
    public List<RiderSeed> riders = List.of();
    public List<DocumentSeed> documents = List.of();
    public List<DisclosureSeed> disclosures = List.of();
    public List<SetSeed> disclosureSets = List.of();
    public List<PincodeSeed> pincodes = List.of();
    public List<OccupationSeed> occupations = List.of();
    public List<NoticeSeed> notices = List.of();
  }

  /** Thrown when seed content would rewrite an immutable row. */
  public static class SeedConflict extends RuntimeException {
    SeedConflict(String message) {
      super(message);
    }
  }

  private final CatalogLoaderDb catalogLoader;
  private final JdbcClient domain;
  private final TransactionTemplate domainTx;

  SeedLoader(CatalogLoaderDb catalogLoader, JdbcClient domain, TransactionTemplate domainTx) {
    this.catalogLoader = catalogLoader;
    this.domain = domain;
    this.domainTx = domainTx;
  }

  @Bean
  @Profile("catalog-sync")
  static CommandLineRunner catalogSync(
      SeedLoader loader, @Value("${surakshasetu.seed.dir}") Path seedDir) {
    return args -> loader.load(seedDir);
  }

  /**
   * Loads {@code seedDir}/catalog/*.yaml in one catalog_loader transaction, then {@code
   * seedDir}/consent/*.yaml in one domain_rw transaction. Returns the rows changed.
   */
  public int load(Path seedDir) {
    List<SeedFile> catalogFiles = read(seedDir.resolve("catalog"));
    List<SeedFile> consentFiles = read(seedDir.resolve("consent"));
    JdbcClient catalog = catalogLoader.jdbc();
    Integer catalogRows =
        catalogLoader
            .tx()
            .execute(
                status ->
                    upsertProducts(catalog, all(catalogFiles, f -> f.products))
                        + upsertRiders(catalog, all(catalogFiles, f -> f.riders))
                        + insertDocuments(catalog, seedDir, all(catalogFiles, f -> f.documents))
                        + upsertDisclosures(catalog, all(catalogFiles, f -> f.disclosures))
                        + insertSets(catalog, all(catalogFiles, f -> f.disclosureSets))
                        + upsertPincodes(catalog, all(catalogFiles, f -> f.pincodes))
                        + upsertOccupations(catalog, all(catalogFiles, f -> f.occupations)));
    Integer noticeRows =
        domainTx.execute(status -> insertNotices(all(consentFiles, f -> f.notices)));
    int changed = catalogRows + noticeRows;
    log.info(
        "seed-catalog: {} rows changed (catalog {}, notices {})", changed, catalogRows, noticeRows);
    return changed;
  }

  private static List<SeedFile> read(Path dir) {
    try (Stream<Path> files = Files.list(dir)) {
      return files
          .filter(p -> p.toString().endsWith(".yaml"))
          .sorted()
          .map(p -> YAML.readValue(p.toFile(), SeedFile.class))
          .toList();
    } catch (IOException e) {
      throw new UncheckedIOException(e);
    }
  }

  private static <T> List<T> all(List<SeedFile> files, Function<SeedFile, List<T>> key) {
    return files.stream().flatMap(f -> key.apply(f).stream()).toList();
  }

  /**
   * Products upsert, except status and effective_to: once a row exists they belong to the kill
   * switch, so a re-seed never puts a withdrawn product back on sale.
   */
  private static int upsertProducts(JdbcClient jdbc, List<ProductSeed> products) {
    int changed = 0;
    for (ProductSeed p : products) {
      changed +=
          jdbc.sql(
                  """
                  INSERT INTO catalog.product AS t (uin, name, category, status, entry_age_min,
                    entry_age_max, maturity_age_max, sa_min_inr, sa_max_inr, term_years,
                    ppt_options, payout_options, rider_uins, effective_from, quote_defaults,
                    launch_enabled, is_dummy)
                  VALUES (:uin, :name, :category, 'in_force', :ageMin, :ageMax, :maturity, :saMin,
                    :saMax, int4range(:termMin, :termMax, '[]'), :ppt, :payout, :riders, :from,
                    CAST(:quoteDefaults AS jsonb), :launch, :dummy)
                  ON CONFLICT (uin) DO UPDATE SET name = EXCLUDED.name,
                    category = EXCLUDED.category, entry_age_min = EXCLUDED.entry_age_min,
                    entry_age_max = EXCLUDED.entry_age_max,
                    maturity_age_max = EXCLUDED.maturity_age_max, sa_min_inr = EXCLUDED.sa_min_inr,
                    sa_max_inr = EXCLUDED.sa_max_inr, term_years = EXCLUDED.term_years,
                    ppt_options = EXCLUDED.ppt_options, payout_options = EXCLUDED.payout_options,
                    rider_uins = EXCLUDED.rider_uins, effective_from = EXCLUDED.effective_from,
                    quote_defaults = EXCLUDED.quote_defaults,
                    launch_enabled = EXCLUDED.launch_enabled, is_dummy = EXCLUDED.is_dummy
                  WHERE (t.name, t.category, t.entry_age_min, t.entry_age_max, t.maturity_age_max,
                      t.sa_min_inr, t.sa_max_inr, t.term_years, t.ppt_options, t.payout_options,
                      t.rider_uins, t.effective_from, t.quote_defaults, t.launch_enabled,
                      t.is_dummy)
                    IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.category, EXCLUDED.entry_age_min,
                      EXCLUDED.entry_age_max, EXCLUDED.maturity_age_max, EXCLUDED.sa_min_inr,
                      EXCLUDED.sa_max_inr, EXCLUDED.term_years, EXCLUDED.ppt_options,
                      EXCLUDED.payout_options, EXCLUDED.rider_uins, EXCLUDED.effective_from,
                      EXCLUDED.quote_defaults, EXCLUDED.launch_enabled, EXCLUDED.is_dummy)
                  """)
              .param("uin", p.uin())
              .param("name", p.name())
              .param("category", p.category())
              .param("ageMin", p.entryAgeMin())
              .param("ageMax", p.entryAgeMax())
              .param("maturity", p.maturityAgeMax())
              .param("saMin", p.saMinInr())
              .param("saMax", p.saMaxInr())
              .param("termMin", p.termYearsMin())
              .param("termMax", p.termYearsMax())
              .param("ppt", p.pptOptions().toArray(String[]::new))
              .param("payout", p.benefitPaymentOptions().toArray(String[]::new))
              .param("riders", p.riderUins().toArray(String[]::new))
              .param("from", p.effectiveFrom())
              .param("quoteDefaults", JSON.writeValueAsString(p.quoteDefaults()))
              .param("launch", p.launchEnabled())
              .param("dummy", p.isDummy())
              .update();
    }
    return changed;
  }

  private static int upsertRiders(JdbcClient jdbc, List<RiderSeed> riders) {
    int changed = 0;
    for (RiderSeed r : riders) {
      changed +=
          jdbc.sql(
                  """
                  INSERT INTO catalog.rider AS t (uin, name, attaches_to, sa_max_inr, is_dummy)
                  VALUES (:uin, :name, :attachesTo, :saMax, :dummy)
                  ON CONFLICT (uin) DO UPDATE SET name = EXCLUDED.name,
                    attaches_to = EXCLUDED.attaches_to, sa_max_inr = EXCLUDED.sa_max_inr,
                    is_dummy = EXCLUDED.is_dummy
                  WHERE (t.name, t.attaches_to, t.sa_max_inr, t.is_dummy)
                    IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.attaches_to, EXCLUDED.sa_max_inr,
                      EXCLUDED.is_dummy)
                  """)
              .param("uin", r.uin())
              .param("name", r.name())
              .param("attachesTo", r.attachesTo().toArray(String[]::new))
              .param("saMax", r.saMaxInr())
              .param("dummy", r.isDummy())
              .update();
    }
    return changed;
  }

  /** Documents are immutable per (uin, kind, version, language): a new file needs a new version. */
  private static int insertDocuments(JdbcClient jdbc, Path seedDir, List<DocumentSeed> documents) {
    int changed = 0;
    for (DocumentSeed d : documents) {
      String uri = "content/seed/" + d.path();
      byte[] sha256;
      try {
        sha256 = HEX.parseHex(Jcs.sha256Hex(Files.readAllBytes(seedDir.resolve(d.path()))));
      } catch (IOException e) {
        throw new UncheckedIOException(e);
      }
      Optional<Boolean> same =
          jdbc.sql(
                  """
                  SELECT uri = :uri AND sha256 = :sha AND is_dummy = :dummy
                  FROM catalog.product_document
                  WHERE uin = :uin AND kind = :kind AND version = :version AND language = :language
                  """)
              .param("uri", uri)
              .param("sha", sha256)
              .param("dummy", d.isDummy())
              .param("uin", d.uin())
              .param("kind", d.kind())
              .param("version", d.version())
              .param("language", d.language())
              .query(Boolean.class)
              .optional();
      if (same.isPresent()) {
        if (!same.get()) {
          throw new SeedConflict(
              "document %s %s %s %s changed: a new document version is required"
                  .formatted(d.uin(), d.kind(), d.version(), d.language()));
        }
        continue;
      }
      changed +=
          jdbc.sql(
                  """
                  INSERT INTO catalog.product_document (uin, kind, version, language, uri, sha256,
                    is_dummy)
                  VALUES (:uin, :kind, :version, :language, :uri, :sha, :dummy)
                  """)
              .param("uin", d.uin())
              .param("kind", d.kind())
              .param("version", d.version())
              .param("language", d.language())
              .param("uri", uri)
              .param("sha", sha256)
              .param("dummy", d.isDummy())
              .update();
    }
    return changed;
  }

  /**
   * A body is immutable under its (disclosure_id, language) (OD-1). The other columns upsert, so a
   * disclosure can be retired with an effective_to.
   */
  private static int upsertDisclosures(JdbcClient jdbc, List<DisclosureSeed> disclosures) {
    int changed = 0;
    for (DisclosureSeed d : disclosures) {
      for (Map.Entry<String, String> body : d.bodies().entrySet()) {
        String language = body.getKey();
        String text = Jcs.nfc(body.getValue());
        Optional<String> stored =
            jdbc.sql("SELECT body FROM catalog.disclosure WHERE disclosure_id = ? AND language = ?")
                .params(d.disclosureId(), language)
                .query(String.class)
                .optional();
        if (stored.isPresent() && !stored.get().equals(text)) {
          throw new SeedConflict(
              "disclosure %s (%s) body changed: a new disclosure_id and a new registry_version"
                      .formatted(d.disclosureId(), language)
                  + " are required");
        }
        changed +=
            jdbc.sql(
                    """
                    INSERT INTO catalog.disclosure AS t (disclosure_id, scope, scope_key, language,
                      body, body_sha256, approved_by, effective_from, effective_to, is_dummy)
                    VALUES (:id, :scope, :scopeKey, :language, :body, :sha, :approvedBy, :from,
                      :to, :dummy)
                    ON CONFLICT (disclosure_id, language) DO UPDATE SET scope = EXCLUDED.scope,
                      scope_key = EXCLUDED.scope_key, approved_by = EXCLUDED.approved_by,
                      effective_from = EXCLUDED.effective_from,
                      effective_to = EXCLUDED.effective_to, is_dummy = EXCLUDED.is_dummy
                    WHERE (t.scope, t.scope_key, t.approved_by, t.effective_from, t.effective_to,
                        t.is_dummy)
                      IS DISTINCT FROM (EXCLUDED.scope, EXCLUDED.scope_key, EXCLUDED.approved_by,
                        EXCLUDED.effective_from, EXCLUDED.effective_to, EXCLUDED.is_dummy)
                    """)
                .param("id", d.disclosureId())
                .param("scope", d.scope())
                .param("scopeKey", d.scopeKey())
                .param("language", language)
                .param("body", text)
                .param("sha", HEX.parseHex(Jcs.bodySha256(text)))
                .param("approvedBy", d.approvedBy())
                .param("from", d.effectiveFrom())
                .param("to", d.effectiveTo())
                .param("dummy", d.isDummy())
                .update();
      }
    }
    return changed;
  }

  /** A set is immutable under (uin, channel, language, registry_version). */
  private static int insertSets(JdbcClient jdbc, List<SetSeed> sets) {
    int changed = 0;
    for (SetSeed s : sets) {
      if (!REGISTRY_VERSION.matcher(s.registryVersion()).matches()) {
        throw new SeedConflict(
            "registry_version %s is not YYYY.MM.N".formatted(s.registryVersion()));
      }
      for (String channel : s.channels()) {
        for (String language : s.languages()) {
          List<Jcs.SetItem> items = new ArrayList<>();
          for (String id : s.disclosureIds()) {
            byte[] bodySha =
                jdbc.sql(
                        "SELECT body_sha256 FROM catalog.disclosure"
                            + " WHERE disclosure_id = ? AND language = ?")
                    .params(id, language)
                    .query(byte[].class)
                    .optional()
                    .orElseThrow(
                        () ->
                            new SeedConflict(
                                "set %s/%s/%s %s names unknown disclosure %s"
                                    .formatted(
                                        s.uin(), channel, language, s.registryVersion(), id)));
            items.add(new Jcs.SetItem(id, HEX.formatHex(bodySha)));
          }
          String setSha256 = Jcs.setSha256(s.registryVersion(), s.uin(), channel, language, items);
          String[] ids = s.disclosureIds().toArray(String[]::new);
          Optional<Boolean> same =
              jdbc.sql(
                      """
                      SELECT disclosure_ids = CAST(:ids AS text[]) AND set_sha256 = :sha
                      FROM catalog.disclosure_set
                      WHERE uin = :uin AND channel = :channel AND language = :language
                        AND registry_version = :version
                      """)
                  .param("ids", ids)
                  .param("sha", HEX.parseHex(setSha256))
                  .param("uin", s.uin())
                  .param("channel", channel)
                  .param("language", language)
                  .param("version", s.registryVersion())
                  .query(Boolean.class)
                  .optional();
          if (same.isPresent()) {
            if (!same.get()) {
              throw new SeedConflict(
                  "disclosure set %s/%s/%s changed under registry_version %s: a new"
                          .formatted(s.uin(), channel, language, s.registryVersion())
                      + " registry_version is required");
            }
            continue;
          }
          changed +=
              jdbc.sql(
                      """
                      INSERT INTO catalog.disclosure_set (uin, channel, language, registry_version,
                        disclosure_ids, set_sha256)
                      VALUES (:uin, :channel, :language, :version, :ids, :sha)
                      """)
                  .param("uin", s.uin())
                  .param("channel", channel)
                  .param("language", language)
                  .param("version", s.registryVersion())
                  .param("ids", ids)
                  .param("sha", HEX.parseHex(setSha256))
                  .update();
        }
      }
    }
    return changed;
  }

  private static int upsertPincodes(JdbcClient jdbc, List<PincodeSeed> pincodes) {
    int changed = 0;
    for (PincodeSeed p : pincodes) {
      changed +=
          jdbc.sql(
                  """
                  INSERT INTO catalog.pincode AS t (pincode, district, state, serviceable, is_dummy)
                  VALUES (?, ?, ?, ?, ?)
                  ON CONFLICT (pincode) DO UPDATE SET district = EXCLUDED.district,
                    state = EXCLUDED.state, serviceable = EXCLUDED.serviceable,
                    is_dummy = EXCLUDED.is_dummy
                  WHERE (t.district, t.state, t.serviceable, t.is_dummy)
                    IS DISTINCT FROM (EXCLUDED.district, EXCLUDED.state, EXCLUDED.serviceable,
                      EXCLUDED.is_dummy)
                  """)
              .params(p.pincode(), p.district(), p.state(), p.serviceable(), p.isDummy())
              .update();
    }
    return changed;
  }

  private static int upsertOccupations(JdbcClient jdbc, List<OccupationSeed> occupations) {
    int changed = 0;
    for (OccupationSeed o : occupations) {
      changed +=
          jdbc.sql(
                  """
                  INSERT INTO catalog.occupation AS t (code, label, risk_class, is_dummy)
                  VALUES (?, ?, ?, ?)
                  ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label,
                    risk_class = EXCLUDED.risk_class, is_dummy = EXCLUDED.is_dummy
                  WHERE (t.label, t.risk_class, t.is_dummy)
                    IS DISTINCT FROM (EXCLUDED.label, EXCLUDED.risk_class, EXCLUDED.is_dummy)
                  """)
              .params(o.code(), o.label(), o.riskClass(), o.isDummy())
              .update();
    }
    return changed;
  }

  /** Notices are immutable (domain_rw has INSERT and SELECT only): new text, new version. */
  private int insertNotices(List<NoticeSeed> notices) {
    int changed = 0;
    for (NoticeSeed n : notices) {
      String body = Jcs.nfc(n.body());
      Optional<Boolean> same =
          domain
              .sql(
                  """
                  SELECT language = ? AND body = ? AND approved_by = ? AND effective_from = ?
                    AND is_dummy = ?
                  FROM consent.notice_version WHERE notice_version = ?
                  """)
              .params(
                  n.language(),
                  body,
                  n.approvedBy(),
                  n.effectiveFrom(),
                  n.isDummy(),
                  n.noticeVersion())
              .query(Boolean.class)
              .optional();
      if (same.isPresent()) {
        if (!same.get()) {
          throw new SeedConflict(
              "notice %s changed: a new notice_version is required".formatted(n.noticeVersion()));
        }
        continue;
      }
      changed +=
          domain
              .sql(
                  """
                  INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,
                    approved_by, effective_from, is_dummy)
                  VALUES (?, ?, ?, ?, ?, ?, ?)
                  """)
              .params(
                  n.noticeVersion(),
                  n.language(),
                  body,
                  HEX.parseHex(Jcs.bodySha256(body)),
                  n.approvedBy(),
                  n.effectiveFrom(),
                  n.isDummy())
              .update();
    }
    return changed;
  }
}
