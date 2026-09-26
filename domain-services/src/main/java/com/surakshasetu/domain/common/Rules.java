package com.surakshasetu.domain.common;

import java.io.IOException;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.jspecify.annotations.Nullable;
import org.kie.dmn.api.core.DMNModel;
import org.kie.dmn.api.core.DMNResult;
import org.kie.dmn.api.core.DMNRuntime;
import org.kie.dmn.core.internal.utils.DMNRuntimeBuilder;
import org.kie.internal.io.ResourceFactory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.dataformat.yaml.YAMLMapper;

/**
 * The rules releases this service evaluates, keyed by rules_version. A release is the eligibility
 * and suitability DMN models of one version (files {@code dmn/<model>-<v>.dmn}, namespace {@code
 * urn:surakshasetu:rules-<v>}) plus the one {@code params/*.yaml} that names it. Every shipped
 * release loads at startup and stays loaded, so a session keeps the version it pinned (I7); a
 * request evaluates exactly its {@code pins.rules}.
 */
@Component
public class Rules {

  private static final Logger log = LoggerFactory.getLogger(Rules.class);
  private static final String NAMESPACE = "urn:surakshasetu:";
  private static final Pattern FILE =
      Pattern.compile("(eligibility|suitability)-(\\d{4}\\.\\d{2}\\.\\d+)\\.dmn");
  // Numeric order on YYYY.MM.N, so rules-2026.10.1 is newer than rules-2026.09.12.
  private static final Comparator<String> NEWEST_FIRST =
      Comparator.comparing(
              (String v) ->
                  Arrays.stream(v.substring("rules-".length()).split("\\."))
                      .mapToInt(Integer::parseInt)
                      .toArray(),
              Arrays::compare)
          .reversed();

  /** Strict YAML for every versioned rules, weights and rate file: unknown or missing keys fail. */
  public static final YAMLMapper YAML =
      YAMLMapper.builder()
          .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
          .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
          .enable(DeserializationFeature.FAIL_ON_MISSING_CREATOR_PROPERTIES)
          .build();

  /** Actuarial assumptions for cover sizing (TDD §3.7); DUMMY until D6. */
  public record ActuarialParams(
      String paramsVersion,
      String rulesVersion,
      boolean isDummy,
      BigDecimal consumptionShare,
      BigDecimal discountRate,
      BigDecimal incomeGrowth,
      Map<String, Integer> independenceAge,
      int coverToAgeDefault,
      BigDecimal finalExpensesInr,
      BigDecimal employerCoverShare,
      BigDecimal saRoundingStepInr,
      List<GoalCorpus> goalCorpora) {}

  /** A corpus in today's rupees per dependant of {@code relation}, due at {@code dueAtAge}. */
  public record GoalCorpus(String goal, String relation, BigDecimal amountInr, int dueAtAge) {}

  public record Release(
      String rulesVersion,
      String paramsVersion,
      DMNRuntime runtime,
      DMNModel eligibility,
      DMNModel suitability,
      ActuarialParams params) {

    /**
     * Evaluates one decision and returns every decision evaluated on the way (the decision and
     * those it requires), by name. A DMN error is a 500; only message types are logged, since a
     * message can quote an input value.
     */
    public Map<String, @Nullable Object> decide(
        DMNModel model, String decision, Map<String, @Nullable Object> inputs) {
      var context = runtime.newContext();
      inputs.forEach(context::set);
      DMNResult result = runtime.evaluateDecisionByName(model, decision, context);
      if (result.hasErrors()) {
        log.error(
            "DMN {}/{} under {} failed: {}",
            model.getName(),
            decision,
            rulesVersion,
            result.getMessages().stream().map(m -> m.getMessageType().name()).toList());
        throw new IllegalStateException("DMN evaluation failed");
      }
      Map<String, @Nullable Object> out = new LinkedHashMap<>();
      result.getDecisionResults().forEach(d -> out.put(d.getDecisionName(), d.getResult()));
      return out;
    }

    /** The rows a decision table returned: none, one (FIRST/UNIQUE) or many (COLLECT). */
    @SuppressWarnings("unchecked")
    public List<Map<String, @Nullable Object>> rows(
        DMNModel model, String decision, Map<String, @Nullable Object> inputs) {
      Object value = decide(model, decision, inputs).get(decision);
      if (value == null) {
        return List.of();
      }
      return value instanceof List<?> list
          ? (List<Map<String, @Nullable Object>>) list
          : List.of((Map<String, @Nullable Object>) value);
    }
  }

  private final Map<String, Release> releases; // newest first

  Rules() throws IOException {
    this("classpath*:dmn/*.dmn", "classpath*:params/*.yaml");
  }

  private Rules(String dmnPattern, String paramsPattern) throws IOException {
    var resolver = new PathMatchingResourcePatternResolver();
    this.releases =
        releases(
            Arrays.asList(resolver.getResources(dmnPattern)),
            Arrays.asList(resolver.getResources(paramsPattern)));
    log.info("rules loaded: {}", releases.keySet());
  }

  /** Loads every DMN and params file the Spring resource patterns match. */
  public static Rules load(String dmnPattern, String paramsPattern) throws IOException {
    return new Rules(dmnPattern, paramsPattern);
  }

  private static Map<String, Release> releases(List<Resource> dmnFiles, List<Resource> paramFiles)
      throws IOException {
    Map<String, List<org.kie.api.io.Resource>> models = new HashMap<>();
    for (Resource file : dmnFiles) {
      Matcher name = FILE.matcher(String.valueOf(file.getFilename()));
      if (!name.matches()) {
        throw new IllegalStateException("DMN file name is not <model>-<version>.dmn: " + file);
      }
      var kie = ResourceFactory.newByteArrayResource(file.getContentAsByteArray());
      kie.setSourcePath("dmn/" + file.getFilename());
      models.computeIfAbsent("rules-" + name.group(2), v -> new ArrayList<>()).add(kie);
    }
    Map<String, ActuarialParams> params = new HashMap<>();
    for (Resource file : paramFiles) {
      ActuarialParams p = YAML.readValue(file.getContentAsByteArray(), ActuarialParams.class);
      if (params.put(p.rulesVersion(), p) != null) {
        throw new IllegalStateException("two params files for " + p.rulesVersion());
      }
    }
    if (models.isEmpty() || !models.keySet().equals(params.keySet())) {
      throw new IllegalStateException(
          "every rules version needs one params file: dmn "
              + models.keySet()
              + ", params "
              + params.keySet());
    }

    Map<String, Release> releases = new TreeMap<>(NEWEST_FIRST);
    for (var entry : models.entrySet()) {
      String version = entry.getKey();
      DMNRuntime runtime =
          DMNRuntimeBuilder.fromDefaults()
              .buildConfiguration()
              .fromResources(entry.getValue())
              .getOrElseThrow(e -> new IllegalStateException("DMN " + version, e));
      for (DMNModel model : runtime.getModels()) {
        if (model.hasErrors()) {
          throw new IllegalStateException(
              "DMN " + version + "/" + model.getName() + ": " + model.getMessages());
        }
      }
      ActuarialParams p = params.get(version);
      releases.put(
          version,
          new Release(
              version,
              p.paramsVersion(),
              runtime,
              model(runtime, version, "eligibility"),
              model(runtime, version, "suitability"),
              p));
    }
    // Newest first, but looked up by hash: a TreeMap would run the version comparator on the
    // pinned string, which throws for one that isn't rules-YYYY.MM.N.
    return new LinkedHashMap<>(releases);
  }

  private static DMNModel model(DMNRuntime runtime, String version, String name) {
    DMNModel model = runtime.getModel(NAMESPACE + version, name);
    if (model == null) {
      throw new IllegalStateException(
          "rules " + version + " has no " + name + " model in namespace " + NAMESPACE + version);
    }
    return model;
  }

  /** The release a request pinned; 409 RULES_VERSION_UNKNOWN when it isn't loaded. */
  public Release release(String rulesVersion) {
    Release release = releases.get(rulesVersion);
    if (release == null) {
      throw Problems.problem(
          HttpStatus.CONFLICT, "RULES_VERSION_UNKNOWN", "the pinned rules version is not loaded");
    }
    return release;
  }

  /** The newest release: what a new session pins. */
  public Release current() {
    return releases.values().iterator().next();
  }

  /** Every loaded rules version, newest first. */
  public List<String> active() {
    return List.copyOf(releases.keySet());
  }

  /** A mutable input map that, unlike {@code Map.of}, takes null values (declined answers). */
  public static Map<String, @Nullable Object> inputs(@Nullable Object... keysAndValues) {
    Map<String, @Nullable Object> inputs = new HashMap<>();
    for (int i = 0; i < keysAndValues.length; i += 2) {
      inputs.put((String) keysAndValues[i], keysAndValues[i + 1]);
    }
    return inputs;
  }
}
