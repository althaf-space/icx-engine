package com.example.api;

import jakarta.inject.Inject;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;

import com.example.service.GreetingService;

@Path("/hello")
@ApplicationScoped
public class GreetingResource {

    @Inject
    GreetingService greetingService;

    @GET
    @Produces(MediaType.TEXT_PLAIN)
    public String hello() {
        return greetingService.greet("World");
    }

    @GET
    @Path("/custom/{name}")
    public String custom(String name) {
        return greetingService.greet(name);
    }
}
