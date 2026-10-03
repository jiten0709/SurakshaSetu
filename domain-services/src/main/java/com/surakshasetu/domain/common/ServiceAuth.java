package com.surakshasetu.domain.common;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.jspecify.annotations.Nullable;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.http.HttpStatus;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Service-to-service auth (Step 16; mTLS in production). Every /v1 operation needs {@code
 * Authorization: Bearer <service token>}, otherwise 401 UNAUTHORIZED. The kill switch is internal
 * scope: it also needs {@code X-Internal-Token}, otherwise 403 FORBIDDEN. Health stays open.
 *
 * <p>It runs before any other interceptor and before the body is read, so an unauthenticated
 * request learns nothing about its payload. Neither token has a default: startup fails without
 * them. Tokens are compared in constant time and never logged.
 */
@Configuration
class ServiceAuth implements WebMvcConfigurer, HandlerInterceptor {

  static final String KILL_SWITCH = "setProductKillSwitch";

  private final byte[] serviceToken;
  private final byte[] internalToken;

  ServiceAuth(
      @Value("${surakshasetu.auth.service-token}") String serviceToken,
      @Value("${surakshasetu.auth.internal-token}") String internalToken) {
    if (serviceToken.isBlank() || internalToken.isBlank() || serviceToken.equals(internalToken)) {
      throw new IllegalStateException("the service and internal tokens must be set and differ");
    }
    this.serviceToken = serviceToken.getBytes(StandardCharsets.UTF_8);
    this.internalToken = internalToken.getBytes(StandardCharsets.UTF_8);
  }

  @Override
  public void addInterceptors(InterceptorRegistry registry) {
    registry.addInterceptor(this).addPathPatterns("/v1/**").order(Ordered.HIGHEST_PRECEDENCE);
  }

  @Override
  public boolean preHandle(
      HttpServletRequest request, HttpServletResponse response, Object handler) {
    if (!(handler instanceof HandlerMethod method)) {
      return true; // no operation here: Spring answers 404 or 405 without reaching a controller
    }
    String authorization = request.getHeader("Authorization");
    String bearer =
        authorization != null && authorization.regionMatches(true, 0, "Bearer ", 0, 7)
            ? authorization.substring(7).strip()
            : null;
    if (!matches(bearer, serviceToken)) {
      throw Problems.problem(HttpStatus.UNAUTHORIZED, "UNAUTHORIZED", "a service token is needed");
    }
    if (KILL_SWITCH.equals(method.getMethod().getName())
        && !matches(request.getHeader("X-Internal-Token"), internalToken)) {
      throw Problems.problem(HttpStatus.FORBIDDEN, "FORBIDDEN", "this operation is internal scope");
    }
    return true;
  }

  private static boolean matches(@Nullable String presented, byte[] expected) {
    return presented != null
        && MessageDigest.isEqual(presented.getBytes(StandardCharsets.UTF_8), expected);
  }
}
