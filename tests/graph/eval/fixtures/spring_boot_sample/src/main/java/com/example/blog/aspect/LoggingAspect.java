package com.example.blog.aspect;

import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.springframework.stereotype.Component;

@Aspect
@Component
public class LoggingAspect {

    @Around("execution(* com.example.blog.service.UserService.*(..))")
    public Object logUserServiceCalls(ProceedingJoinPoint joinPoint) throws Throwable {
        System.out.println("Calling: " + joinPoint.getSignature());
        return joinPoint.proceed();
    }
}
